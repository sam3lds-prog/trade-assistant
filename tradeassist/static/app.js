"use strict";

/* ---------- small helpers ---------- */
const SVG_NS = "http://www.w3.org/2000/svg";

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let resp;
  try { resp = await fetch(path, opts); }
  catch (e) { throw new Error("Cannot reach the app server. Is it still running?"); }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* not JSON */ }
  if (!resp.ok) {
    const detail = data && (data.error || (Array.isArray(data.detail)
      ? data.detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ") : data.detail));
    throw new Error(detail || `Request failed (${resp.status})`);
  }
  return data;
}

const isNum = v => typeof v === "number" && isFinite(v);
// Rounded before the sign is chosen, so -0.3 shown with no decimals reads "$0", not "-$0".
const money = (v, d = 2) => {
  if (!isNum(v)) return "n/a";
  const r = Number(v.toFixed(d));
  return (r < 0 ? "-$" : "$") + Math.abs(r).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
};
const num = (v, d = 2) => isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "n/a";
const pct = (v, d = 1) => isNum(v) ? `${v.toFixed(d)}%` : "n/a";
const signedPct = (v, d = 1) => isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(d)}%` : "n/a";
const shortDate = iso => {
  if (!iso) return "n/a";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

const STATUS_WORDS = { pass: "Pass", warn: "Caution", fail: "Red flag", info: "Note" };
function statusTag(status, word) {
  const shapes = {
    pass: svg("circle", { cx: 6, cy: 6, r: 5, fill: "currentColor" }),
    warn: svg("path", { d: "M6 1 L11.5 11 L0.5 11 Z", fill: "currentColor" }),
    fail: svg("rect", { x: 1, y: 1, width: 10, height: 10, fill: "currentColor" }),
    info: svg("rect", { x: 1, y: 4.5, width: 10, height: 3, fill: "currentColor" }),
  };
  const icon = svg("svg", { width: 12, height: 12, viewBox: "0 0 12 12", "aria-hidden": "true" }, shapes[status]);
  return el("span", { class: `status-tag ${status}` }, icon, word || STATUS_WORDS[status]);
}

function banner(level, text) { return el("div", { class: `banner ${level}`, role: level === "fail" ? "alert" : null }, text); }

async function withBusy(button, errorBox, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  if (errorBox) errorBox.textContent = "";
  try { await fn(); }
  catch (e) { if (errorBox) errorBox.textContent = e.message; else console.error(e); }
  finally { button.disabled = false; button.textContent = label; }
}

function field(label, control) {
  // A group of radio buttons has its own labels inside, and labels must not be nested.
  if (control.classList && control.classList.contains("seg")) {
    control.setAttribute("aria-label", label);
    return el("div", { class: "field" }, el("span", { text: label }), control);
  }
  return el("label", { class: "field" }, el("span", { text: label }), control);
}

function segmented(name, options, value) {
  const wrap = el("div", { class: "seg", role: "radiogroup" });
  for (const [val, text] of options) {
    wrap.append(el("label", {}, el("input", { type: "radio", name, value: val, checked: val === value }), text));
  }
  wrap.value = () => wrap.querySelector("input:checked").value;
  return wrap;
}

function table(headers, rows, opts = {}) {
  const thead = el("thead", {}, el("tr", {}, headers.map(h => el("th", { class: h.num ? "num" : null, text: h.label }))));
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr", { class: opts.onRow ? "click" : null, tabindex: opts.onRow ? "0" : null },
      headers.map(h => {
        const v = h.cell(row);
        return el("td", { class: h.num ? "num" : null }, v === null || v === undefined ? "n/a" : v);
      }));
    if (opts.onRow) {
      tr.addEventListener("click", () => opts.onRow(row, tr));
      tr.addEventListener("keydown", e => { if (e.key === "Enter") opts.onRow(row, tr); });
    }
    tbody.append(tr);
  }
  return el("div", { class: "scroll" }, el("table", {}, thead, tbody));
}

/* ---------- line chart with hover ---------- */
function lineChart({ series, events = [], yFormat = v => num(v, 0), height = 260 }) {
  const W = 760, H = height, pad = { l: 70, r: 92, t: 12, b: 26 };
  const all = series.flatMap(s => s.points.filter(p => isNum(p.y)).map(p => p.y));
  const xs = series[0].points.map(p => p.x);
  const wrap = el("div", { class: "chart" });
  if (!all.length || xs.length < 2) { wrap.append(el("p", { class: "muted", text: "Not enough data to draw." })); return wrap; }
  let lo = Math.min(...all), hi = Math.max(...all);
  const span = (hi - lo) || Math.abs(hi) || 1;
  const rough = span / 4, mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= rough);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(v);
  const X = i => pad.l + (W - pad.l - pad.r) * i / (xs.length - 1);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Line chart of ${series.map(s => s.name).join(", ")}` });
  for (const v of ticks) {
    root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
      svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: yFormat(v) }));
  }
  const labelEvery = Math.max(1, Math.floor(xs.length / 5));
  for (let i = 0; i < xs.length; i += labelEvery) {
    if (X(i) > W - pad.r - 30) break;
    root.append(svg("text", { x: X(i), y: H - 8, "text-anchor": "middle", text: xs[i].slice(0, 7) }));
  }
  const ends = [];
  for (const s of series) {
    let d = "", pen = false, last = null;
    s.points.forEach((p, i) => {
      if (!isNum(p.y)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)} ${Y(p.y).toFixed(1)} `;
      pen = true; last = { i, y: p.y };
    });
    root.append(svg("path", { d, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    if (last) ends.push({ name: s.name, y: Y(last.y) });
  }
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  for (const e of ends) root.append(svg("text", { x: W - pad.r + 8, y: e.y + 4, text: e.name }));
  const index = new Map(xs.map((x, i) => [x, i]));
  for (const ev of events) {
    const i = index.get(ev.date);
    const p = series[0].points[i];
    if (i === undefined || !isNum(p.y)) continue;
    const x = X(i), y = Y(p.y), up = ev.direction >= 0;
    root.append(svg("path", {
      d: up ? `M${x} ${y + 8} L${x - 5} ${y + 17} L${x + 5} ${y + 17} Z` : `M${x} ${y - 8} L${x - 5} ${y - 17} L${x + 5} ${y - 17} Z`,
      fill: "var(--ink)", stroke: "var(--panel)", "stroke-width": 1.5 }));
  }
  const hoverLine = svg("line", { class: "hover-line", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
  const dots = series.map(s => svg("circle", { r: 4, fill: `var(${s.color})`, stroke: "var(--panel)", "stroke-width": 2, visibility: "hidden" }));
  root.append(hoverLine, ...dots);
  const tip = el("div", { class: "tip" });
  const move = evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(xs.length - 1, Math.round((px - pad.l) / (W - pad.l - pad.r) * (xs.length - 1))));
    hoverLine.setAttribute("x1", X(i)); hoverLine.setAttribute("x2", X(i)); hoverLine.setAttribute("visibility", "visible");
    tip.replaceChildren(el("strong", { text: shortDate(xs[i]) }));
    series.forEach((s, k) => {
      const p = s.points[i];
      if (isNum(p.y)) { dots[k].setAttribute("cx", X(i)); dots[k].setAttribute("cy", Y(p.y)); dots[k].setAttribute("visibility", "visible"); }
      else dots[k].setAttribute("visibility", "hidden");
      tip.append(el("div", {}, `${s.name}: ${yFormat(p.y)}`));
    });
    tip.style.display = "block";
    const left = X(i) * box.width / W;
    tip.style.left = `${left > box.width * 0.6 ? left - tip.offsetWidth - 12 : left + 12}px`;
    tip.style.top = "8px";
  };
  root.addEventListener("pointermove", move);
  root.addEventListener("pointerleave", () => {
    hoverLine.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden"));
    tip.style.display = "none";
  });
  if (series.length > 1) {
    wrap.append(el("div", { class: "legend" }, series.map(s =>
      el("span", {}, el("i", { style: `background: var(${s.color})` }), s.name))));
  }
  wrap.append(root, tip);
  return wrap;
}

/* ---------- the spread ruler ---------- */
function spreadRuler(ladder) {
  const W = 760, left = 70, right = 690;
  const span = ladder.ask - ladder.bid;
  const X = p => span > 0 ? left + (right - left) * (p - ladder.bid) / span : (left + right) / 2;
  // Labels above the line are staggered when two prices sit close together. Work the levels out
  // first, left to right, so the drawing can be made tall enough for the highest one.
  const placed = ladder.rungs.map(rung => ({ rung, x: X(rung.price), level: 0 })).sort((a, b) => a.x - b.x);
  placed.forEach((r, i) => { if (i && r.x - placed[i - 1].x < 110) r.level = (placed[i - 1].level + 1) % 3; });
  const base = 84 + 34 * Math.max(0, ...placed.map(r => r.level)), H = base + 72;
  const root = svg("svg", { class: "ruler", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Bid ${money(ladder.bid)}, ask ${money(ladder.ask)}, suggested limits ${ladder.rungs.map(r => money(r.price)).join(", ")}` });
  root.append(svg("line", { class: "axis", x1: left, x2: right, y1: base, y2: base }));
  if (ladder.grid && ladder.grid.length <= 41) {
    for (const p of ladder.grid) root.append(svg("line", { class: "tick", x1: X(p), x2: X(p), y1: base - 5, y2: base + 5 }));
  }
  for (const [p, anchor, word] of [[ladder.bid, "end", "Bid"], [ladder.ask, "start", "Ask"]]) {
    const x = X(p) + (anchor === "end" ? -10 : 10);
    root.append(svg("line", { class: "axis", x1: X(p), x2: X(p), y1: base - 9, y2: base + 9 }),
      svg("text", { x, y: base - 2, "text-anchor": anchor, class: "lbl-muted", text: word }),
      svg("text", { x, y: base + 15, "text-anchor": anchor, "font-size": 15, "font-weight": 600, text: money(p) }));
  }
  // Reference marks below the line: midpoint and model value.
  const refs = [{ p: ladder.mid, text: `Midpoint ${money(ladder.mid)}` }];
  if (isNum(ladder.model_value)) {
    const clamped = Math.min(Math.max(ladder.model_value, ladder.bid), ladder.ask);
    const outside = clamped !== ladder.model_value;
    refs.push({ p: clamped, text: `Model value ${money(ladder.model_value)}${outside ? " (outside the quote)" : ""}`, diamond: true });
  }
  refs.sort((a, b) => a.p - b.p);
  refs.forEach((r, i) => {
    const x = X(r.p), close = refs.length === 2 && Math.abs(X(refs[0].p) - X(refs[1].p)) < 170;
    const y = base + 34 + (close && i === 1 ? 20 : 0);
    root.append(svg("line", { class: "ref", x1: x, x2: x, y1: base + 2, y2: y - 12, "stroke-dasharray": r.diamond ? "3 3" : null }));
    if (r.diamond) root.append(svg("path", { d: `M${x} ${base - 5} L${x + 5} ${base} L${x} ${base + 5} L${x - 5} ${base} Z`, fill: "var(--panel)", stroke: "var(--muted)", "stroke-width": 1.5 }));
    const anchor = x > 560 ? "end" : x < 200 ? "start" : "middle";
    root.append(svg("text", { x, y, "text-anchor": anchor, class: "lbl-muted", text: r.text }));
  });
  // Suggested prices above the line.
  for (const { rung, x, level } of placed) {
    const top = base - 44 - level * 34;
    root.append(svg("line", { class: "stem", x1: x, x2: x, y1: base, y2: top + 8 }),
      svg("circle", { class: "rung-dot", cx: x, cy: base, r: 6 }),
      svg("text", { x, y: top, "text-anchor": "middle", class: "rung-price", text: money(rung.price) }),
      svg("text", { x, y: top - 18, "text-anchor": "middle", class: "rung-name", text: rung.name }));
  }
  return root;
}

/* ---------- app state and navigation ---------- */
const state = { status: null, views: {} };
// The version this page was loaded as (the server stamps it on the script address).
const PAGE_VERSION = (() => {
  try { return (new URL(document.currentScript.src).searchParams.get("v") || "").split("-")[0] || "unknown"; }
  catch (e) { return "unknown"; }
})();
const main = document.getElementById("main");

function show(name) {
  for (const btn of document.querySelectorAll(".rail button.nav")) {
    if (btn.dataset.view === name) btn.setAttribute("aria-current", "page"); else btn.removeAttribute("aria-current");
  }
  if (!state.views[name]) { state.views[name] = BUILDERS[name](); main.append(state.views[name]); }
  for (const [key, node] of Object.entries(state.views)) node.classList.toggle("hidden", key !== name);
  if (name === "journal") state.refreshJournal();
  if (state.views[name].onShow) state.views[name].onShow();
  location.hash = name;
}

async function loadStatus() {
  const box = document.getElementById("status");
  try {
    const s = state.status = await api("/api/status");
    const clock = { open: "Market open", closed: "Market closed", pre: "Pre-market", post: "After hours", premarket: "Pre-market", postmarket: "After hours" }[s.clock.state] || "Market hours unknown";
    box.replaceChildren(el("strong", { text: s.data.label }), el("span", { text: s.data.realtime ? "Real-time quotes" : s.data.mode === "demo" ? "Made-up prices" : "Delayed 15 minutes" }),
      el("div", { text: clock }), el("div", { text: "Advisory only: this app never places orders." }),
      el("div", { text: PAGE_VERSION === s.version ? `Version ${s.version}`
        : `This page is version ${PAGE_VERSION} but the app is ${s.version}. Reload the page.` }));
  } catch (e) { box.textContent = e.message; }
}

/* ---------- view: price an option ---------- */
function buildPrice() {
  const sym = el("input", { class: "sym", type: "text", value: "AAPL", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const type = segmented("ptype", [["call", "Call"], ["put", "Put"]], "call");
  const side = segmented("pside", [["buy", "Buy"], ["sell", "Sell"]], "buy");
  const expiry = el("select", { disabled: true }, el("option", { text: "Enter a ticker first" }));
  const strike = el("select", { disabled: true }, el("option", { text: "Choose an expiration" }));
  const contracts = el("input", { type: "number", min: 1, max: 1000, step: 1, value: 1 });
  const earnings = el("input", { type: "date" });
  const go = el("button", { class: "primary", text: "Price it" });
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const note = el("div", { class: "loadnote small muted", "aria-live": "polite" });
  let chain = null, loadedFor = "", ticket = 0;      // ticket: only the newest request may update the form

  const working = text => { note.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text })); go.disabled = true; };
  const settled = text => { note.replaceChildren(el("span", { text })); go.disabled = false; };
  const placeholder = (select, text) => { select.replaceChildren(el("option", { text })); select.disabled = true; };

  async function loadExpirations() {
    const symbol = sym.value.trim().toUpperCase();
    if (!symbol || symbol === loadedFor) return;
    const mine = ++ticket;
    error.textContent = "";
    chain = null; loadedFor = "";
    placeholder(expiry, `Loading ${symbol}…`);
    placeholder(strike, "Waiting for expirations");
    working(`Loading ${symbol} expirations…`);
    try {
      const data = await api(`/api/expirations?symbol=${encodeURIComponent(symbol)}`);
      if (mine !== ticket) return;                    // the ticker was changed again while this was on its way
      loadedFor = data.symbol || symbol;
      sym.value = loadedFor;
      expiry.replaceChildren(...data.expirations.map(e => el("option", { value: e.date, text: `${shortDate(e.date)} (${e.dte} days)` })));
      const pick = data.expirations.find(e => e.dte >= 30) || data.expirations[data.expirations.length - 1];
      expiry.value = pick.date;
      expiry.disabled = false;
      await loadStrikes();
    } catch (e) {
      if (mine !== ticket) return;
      loadedFor = "";
      placeholder(expiry, `Could not load ${symbol}`);
      placeholder(strike, "No strikes");
      settled("");
      error.textContent = e.message;
    }
  }
  async function loadStrikes() {
    const mine = ++ticket, symbol = loadedFor, date = expiry.value;
    error.textContent = "";
    chain = null;
    placeholder(strike, `Loading ${symbol} ${shortDate(date)}…`);
    working(`Loading every ${symbol} ${shortDate(date)} contract and its quote…`);
    try {
      const data = await api(`/api/chain?symbol=${encodeURIComponent(symbol)}&expiration=${date}&width_pct=1000`);
      if (mine !== ticket) return;
      chain = data;
      chain.loaded_at = new Date().toISOString();
      fillStrikes();
    } catch (e) {
      if (mine !== ticket) return;
      placeholder(strike, "Could not load strikes");
      settled("");
      error.textContent = e.message;
    }
  }
  function fillStrikes() {
    if (!chain) return;
    const keep = strike.value, kind = type.value();
    const rows = chain.contracts.filter(c => c.option_type === kind);
    const label = k => num(k, k % 1 ? 2 : 0);
    strike.replaceChildren(...rows.map(c => el("option", { value: c.strike,
      text: `${label(c.strike)}   ${c.bid == null && c.ask == null ? "no quote" : `bid ${num(c.bid)} / ask ${num(c.ask)}`}` })));
    const strikes = rows.map(c => c.strike);
    if (strikes.includes(Number(keep))) strike.value = keep;
    else if (strikes.length) strike.value = strikes.reduce((a, b) => Math.abs(b - chain.spot) < Math.abs(a - chain.spot) ? b : a);
    strike.disabled = !strikes.length;
    settled(strikes.length
      ? `Showing ${chain.symbol} ${shortDate(chain.expiration)} ${kind}s: ${strikes.length} strikes from ${label(Math.min(...strikes))} to ${label(Math.max(...strikes))}. ` +
        `Stock ${money(chain.spot)}. Loaded ${clockTime(chain.loaded_at)} New York time.`
      : `${chain.symbol} has no ${kind}s listed for ${shortDate(chain.expiration)}.`);
  }
  sym.addEventListener("change", loadExpirations);
  sym.addEventListener("keydown", e => { if (e.key === "Enter") loadExpirations(); });
  sym.addEventListener("input", () => {
    // While the box holds a ticker that has not been loaded, the lists below still belong to the old one.
    const typed = sym.value.trim().toUpperCase(), stale = typed !== loadedFor;
    if (!loadedFor || !chain) return;
    expiry.disabled = strike.disabled = stale;
    if (stale) note.replaceChildren(el("span", { text: typed ? `Press Enter to load ${typed}. The lists still show ${loadedFor}.` : `The lists still show ${loadedFor}.` }));
    else fillStrikes();
  });
  expiry.addEventListener("change", loadStrikes);
  type.addEventListener("change", fillStrikes);

  const request = extra => ({
    symbol: loadedFor, expiration: expiry.value, option_type: type.value(), strike: Number(strike.value),
    contracts: Number(contracts.value) || 1, earnings_date: earnings.value || null, ...extra,
  });

  go.addEventListener("click", () => withBusy(go, error, async () => {
    if (sym.value.trim().toUpperCase() !== loadedFor) await loadExpirations();
    if (!loadedFor || strike.disabled) throw new Error("Choose a ticker, expiration and strike first.");
    // Freeze the contract that was priced: live updates and the ticket must keep referring to
    // it even if the form above is changed afterwards.
    const priced = request({ side: side.value() });
    const data = await api("/api/price", priced);
    out.replaceChildren(renderPrice(data, extra => ({ ...priced, ...extra })));
  }));

  const view = el("section", {},
    el("h2", { text: "Price an option" }),
    el("p", { class: "lede", text: "Pick the contract you are looking at in Robinhood. You get limit prices that sit on valid price increments inside the spread, a plan for walking the order, and a set of risk checks." }),
    el("div", { class: "panel" },
      el("div", { class: "row" },
        field("Ticker", sym), field("Type", type), field("Expiration", expiry), field("Strike", strike),
        field("Order", side), field("Contracts", contracts), field("Next earnings date (if you know it)", earnings), go),
      note, error),
    out);
  // Arrive on a contract chosen elsewhere (2.5): fill the form and price it, so the limit ladder is the
  // first thing shown. Changing the type or expiration afterwards reloads the strikes as usual.
  view.loadContract = async spec => {
    error.textContent = "";
    const pick = (seg, v) => { const input = seg.querySelector(`input[value="${v}"]`); if (input) input.checked = true; };
    sym.value = spec.symbol;
    pick(type, spec.option_type); pick(side, spec.side || "buy");
    contracts.value = String(spec.contracts || 1);
    if (spec.symbol !== loadedFor) await loadExpirations();
    if (!loadedFor) return;                             // loading failed; the error is already shown
    if (expiry.value !== spec.expiration) {
      if (![...expiry.options].some(o => o.value === spec.expiration)) {
        error.textContent = `${spec.expiration} is no longer a listed ${spec.symbol} expiration. Choose one above.`;
        return;
      }
      expiry.value = spec.expiration;
      await loadStrikes();
    } else fillStrikes();
    const wanted = [...strike.options].find(o => Number(o.value) === Number(spec.strike));
    if (!wanted) { error.textContent = `The ${spec.strike} ${spec.option_type} is not listed for ${shortDate(spec.expiration)}. Choose a strike above.`; return; }
    strike.value = wanted.value;
    go.click();
  };
  const pending = state.pendingPrice;
  state.pendingPrice = null;
  if (pending) view.loadContract(pending);
  else loadExpirations();
  return view;
}

const NY_CLOCK = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour12: false,
  hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3 });
const clockTime = iso => iso ? NY_CLOCK.format(new Date(iso)) : "unknown";
function ageText(ms) {
  if (!isNum(ms)) return "an unknown time";
  if (ms < 0) ms = 0;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 120000) return `${(ms / 1000).toFixed(1)} s`;
  if (ms < 7200000) return `${Math.round(ms / 60000)} min`;
  return `${(ms / 3600000).toFixed(1)} hours`;
}
function quoteStamp(q) {
  // q: bid_time, ask_time, received_at, mode
  const newest = Math.max(q.bid_time ? Date.parse(q.bid_time) : 0, q.ask_time ? Date.parse(q.ask_time) : 0);
  const age = newest ? Date.parse(q.received_at) - newest : null;
  const source = q.mode === "demo" ? "Made-up prices. " : q.mode === "sandbox" ? "Delayed 15 minutes. " : "";
  if (!newest) return `${source}The data source gave no time for this quote, so its age is unknown. ` +
    `Fetched ${clockTime(q.received_at)} New York time. Check it against your broker's screen.`;
  return `${source}Bid last changed ${clockTime(q.bid_time)}, ask ${clockTime(q.ask_time)} New York time. ` +
    `Fetched ${clockTime(q.received_at)}, ${ageText(age)} after the quote last changed.`;
}

/* The limit-price section. Rebuilt on every live update, so it must not hold any input. */
function limitSection(v) {
  const c = v.contract, lad = v.ladder, nodes = [];
  nodes.push(el("div", { class: "price-head" },
    el("span", { class: "name", text: `${v.symbol} ${shortDate(c.expiration)} ${num(c.strike, c.strike % 1 ? 2 : 0)} ${c.option_type}` }),
    el("span", { class: "muted", text: `Stock ${money(v.spot)}` }),
    el("span", { class: "muted", text: `Quote size ${num(v.bidsize, 0)} × ${num(v.asksize, 0)}` }),
    isNum(c.iv) ? el("span", { class: "muted", text: `Implied volatility ${pct(c.iv * 100, 0)}` }) : null));
  for (const f of lad.flags) nodes.push(banner(f.level, f.text));
  if (!lad.ok) return nodes;
  nodes.push(spreadRuler(lad));
  nodes.push(el("p", { class: "small muted", text: `Spread ${money(lad.spread)} (${pct(lad.spread_pct)} of the midpoint). Price increments: ${lad.tick_rule.evidence}.` }));
  const far = v.side === "buy" ? "paying the ask" : "hitting the bid";
  nodes.push(table([
    { label: "Approach", cell: r => r.name[0].toUpperCase() + r.name.slice(1) },
    { label: "Limit price", num: true, cell: r => el("strong", { text: money(r.price) }) },
    { label: "Across the spread", num: true, cell: r => pct(r.position * 100, 0) },
    { label: `Saves vs ${far}`, num: true, cell: r => money(r.saving_vs_crossing, 0) },
    { label: "Your fill rate here", cell: r => r.observed_fill_rate ? `${pct(r.observed_fill_rate.fill_rate * 100, 0)} of ${r.observed_fill_rate.attempts} tries` : "no history yet" },
    { label: "What to expect", cell: r => r.note },
  ], lad.rungs));
  nodes.push(el("h4", { text: "Walking the order" }),
    el("div", { class: "steps" }, lad.walk.steps.flatMap((s, i) => [i ? el("span", { class: "muted", text: "then" }) : null, el("span", { class: "step", text: money(s) })])),
    el("p", { class: "small", text: lad.walk.instructions }),
    el("p", { class: "small muted", text: `${v.calibration_note} No one can tell you the chance a price inside the spread fills; your own logged attempts are the only real measure.` }));
  return nodes;
}

const LIVE_LIMIT_MS = 15 * 60 * 1000;

function renderPrice(data, request) {
  const c = data.contract, frag = el("div");
  for (const w of data.warnings) frag.append(banner(w.level, w.text));
  const view = { symbol: data.symbol, contract: c, side: data.side, contracts: data.contracts,
    calibration_note: data.calibration_note, spot: data.underlying.spot, bidsize: c.bidsize, asksize: c.asksize, ladder: data.ladder };
  const body = el("div");
  body.replaceChildren(...limitSection(view));

  // Live updating: one small request per interval for this contract's newest quote.
  const seconds = data.data.poll_seconds || 1;
  const open = data.data.market_state === "open";
  const toggle = el("input", { type: "checkbox", checked: open });
  const stamp = el("span", { class: "small muted", text: quoteStamp({ bid_time: c.bid_time, ask_time: c.ask_time, received_at: data.data.received_at, mode: data.data.mode }) });
  const note = el("span", { class: "small muted", text: open ? `Updating every ${seconds} s.` : "The market is closed, so this quote is not changing. Tick the box to keep checking anyway." });
  let timer = null, busy = false, failures = 0, startedAt = Date.now();
  const halt = message => { clearInterval(timer); timer = null; toggle.checked = false; if (message) note.textContent = message; };
  async function tick() {
    if (!body.isConnected) { clearInterval(timer); timer = null; return; }       // a new result replaced this one
    if (document.hidden || busy) return;                                           // no requests for a tab nobody is looking at
    if (Date.now() - startedAt > LIVE_LIMIT_MS) return halt("Paused after 15 minutes to save data requests. Tick the box to resume.");
    busy = true;
    try {
      const live = await api("/api/live", request({ side: data.side }));
      failures = 0;
      if (!body.isConnected) return;
      body.replaceChildren(...limitSection({ ...view, spot: live.spot, bidsize: live.bidsize, asksize: live.asksize, ladder: live.ladder }));
      stamp.textContent = quoteStamp(live);
      if (toggle.checked) note.textContent = `Updating every ${seconds} s. The risk checks below are from ${clockTime(data.data.received_at)}; press Price it to redo them.`;
    } catch (e) {
      failures += 1;
      if (failures >= 3) halt(`Stopped after three failed updates: ${e.message}`);
      else note.textContent = `Update failed (${e.message}). Trying again.`;
    } finally { busy = false; }
  }
  const begin = () => { startedAt = Date.now(); failures = 0; if (!timer) timer = setInterval(tick, Math.max(500, seconds * 1000)); };
  toggle.addEventListener("change", () => {
    if (toggle.checked) { begin(); note.textContent = `Updating every ${seconds} s.`; tick(); }
    else { clearInterval(timer); timer = null; note.textContent = "Updates paused."; }
  });
  if (data.ladder.ok && open) begin();

  const simulateBtn = el("button", { class: "plain", type: "button", text: "Simulate this trade" });
  simulateBtn.addEventListener("click", () => {
    const priced = request({}), rungs = data.ladder.rungs || [];
    const balanced = (rungs.find(r => r.name.includes("balanced")) || rungs[0] || {}).price;
    state.openSimulation(priced.symbol, [{ expiration: priced.expiration, option_type: priced.option_type, strike: priced.strike,
      side: priced.side, contracts: priced.contracts, entry_price: isNum(balanced) ? balanced : null }]);
  });
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h3", { text: data.side === "buy" ? "Where to set your buy limit" : "Where to set your sell limit" }),
      data.ladder.ok ? simulateBtn : null),
    data.ladder.ok ? el("div", { class: "live" }, el("label", {}, toggle, " Keep this quote updating"), note, stamp) : null,
    body);
  frag.append(panel);
  if (!data.ladder.ok) return frag;

  if (!data.scorecard) return frag;
  const card = data.scorecard;
  const verdictClass = card.verdict;
  const scorePanel = el("div", { class: "panel" },
    el("h3", { text: "Risk checks" }),
    banner(verdictClass, `${card.summary} These checks assume a limit of ${money(data.assumed_limit)} on ${data.contracts} contract${data.contracts === 1 ? "" : "s"}.`));
  const grid = el("div", { class: "checks" });
  let group = "";
  for (const chk of card.checks) {
    if (chk.group !== group) { group = chk.group; grid.append(el("div", { class: "group", text: group })); }
    grid.append(el("div", {}, statusTag(chk.status)), el("div", { text: chk.label }),
      el("div", { class: "value" }, el("strong", { text: chk.value })), el("div", { class: "detail small muted", text: chk.detail }));
  }
  scorePanel.append(grid);
  if (data.earnings) scorePanel.append(el("p", { class: "small muted", text: `Earnings date used: ${shortDate(data.earnings.date)} (${data.earnings.source}).` }));
  if (data.recent_patterns.length) {
    scorePanel.append(el("h4", { text: `Chart patterns in the last five trading days (bars through ${shortDate(data.bars_as_of)})` }),
      el("ul", { class: "plain small" }, data.recent_patterns.map(p => el("li", {},
        `${p.label}, ${shortDate(p.date)}. ${p.with_trade ? "Points the same way as this trade." : "Points against this trade."} `,
        el("span", { class: "muted", text: labVerdictText(p.verdicts) })))));
  }
  for (const w of data.data_warnings || []) scorePanel.append(banner("warn", w));
  frag.append(scorePanel);
  frag.append(ticketPanel(data, request));
  return frag;
}

function labVerdictText(verdicts) {
  if (!verdicts || !verdicts.length) return "Not yet tested in the pattern lab.";
  const ok = verdicts.filter(v => v.verdict === "supported").map(v => `${v.horizon} days`);
  if (ok.length) return `Pattern lab: supported at ${ok.join(", ")}.`;
  if (verdicts.some(v => v.verdict === "contradicted")) return "Pattern lab: pointed the wrong way in your data.";
  return "Pattern lab: no evidence it works in your data.";
}

function ticketPanel(data, request) {
  const lad = data.ladder;
  const rung = name => (lad.rungs.find(r => r.name.includes(name)) || lad.rungs[lad.rungs.length - 1]).price;
  const start = el("input", { type: "number", step: "0.01", min: "0.01", value: rung("patient").toFixed(2) });
  const cap = el("input", { type: "number", step: "0.01", min: "0.01", value: rung("fast").toFixed(2) });
  const thesis = el("textarea", { placeholder: "What do you expect to happen, by when, and what would prove you wrong?" });
  const target = el("input", { type: "number", step: "5", min: "5", value: 50 });
  const stop = el("input", { type: "number", step: "5", min: "5", max: "100", value: 50 });
  const exitDays = el("input", { type: "number", step: "1", min: "0", value: 7 });
  const check = el("button", { class: "plain", text: "Check this ticket" });
  const save = el("button", { class: "primary", text: "Save to journal", disabled: true });
  const error = el("div", { class: "error", role: "alert" });
  const result = el("div");
  const overrides = new Set();

  const body = saveIt => request({
    limit_price: Number(start.value), max_limit: Number(cap.value), thesis: thesis.value,
    profit_target_pct: Number(target.value), stop_loss_pct: Number(stop.value),
    exit_days_before_expiry: Number(exitDays.value), overrides: [...overrides], save: saveIt,
  });
  function renderTicket(t) {
    const parts = [];
    const p = t.plan;
    parts.push(el("div", { class: "metrics" },
      metric(money(p.premium_at_risk, 0), "Most you can lose"),
      metric(money(p.profit_target_price), `Take profit (+${p.profit_target_pct}%)`),
      metric(money(p.stop_price), `Stop (-${p.stop_loss_pct}%)`),
      metric(shortDate(p.exit_by), "Out by this date")));
    if (t.violations.length) {
      parts.push(el("h4", { text: "Limits this ticket breaks" }));
      for (const v of t.violations) {
        const box = el("input", { type: "checkbox", checked: overrides.has(v.key) });
        box.addEventListener("change", () => { box.checked ? overrides.add(v.key) : overrides.delete(v.key); save.disabled = true; });
        parts.push(el("div", { class: "banner fail" }, el("strong", { text: `${v.label}: ${v.value}. ` }), v.text,
          v.overridable ? el("label", { class: "small", style: "display:block;margin-top:6px" }, box, " I am breaking this rule on purpose. Record it in the journal.")
            : el("div", { class: "small", style: "margin-top:6px", text: "This one cannot be overridden. Change the ticket, or change the limit in your .env file." })));
      }
    } else if (t.risk_notes && t.risk_notes.length) {
      parts.push(banner("warn", "No limit is broken by what could be measured, but not everything could be checked:"));
      for (const n of t.risk_notes) parts.push(el("div", { class: "banner warn" }, el("strong", { text: `${n.label}: ${n.value}. ` }), n.text));
    } else parts.push(banner("pass", "This ticket is inside all of your limits."));
    if (t.saved_id) parts.push(banner("pass", `Saved to the journal as trade ${t.saved_id}. Enter the order in Robinhood yourself, then log each limit price you try.`));
    result.replaceChildren(...parts);
    save.disabled = !t.can_save || Boolean(t.saved_id);
  }
  check.addEventListener("click", () => withBusy(check, error, async () => renderTicket(await api("/api/ticket", body(false)))));
  save.addEventListener("click", () => withBusy(save, error, async () => renderTicket(await api("/api/ticket", body(true)))));
  for (const input of [start, cap, thesis, target, stop, exitDays]) input.addEventListener("input", () => { save.disabled = true; });

  return el("div", { class: "panel" },
    el("h3", { text: "Trade ticket" }),
    el("p", { class: "small muted", text: "Write the plan down before you place the order. The ticket is sized and checked at the most you are willing to pay." }),
    el("div", { class: "row" }, field("Start the order at", start), field("Most you will pay", cap),
      field("Take profit at (% gain)", target), field("Stop at (% loss)", stop), field("Exit this many days before expiration", exitDays)),
    el("div", { style: "margin-top:12px" }, field("Thesis", thesis)),
    el("div", { class: "row", style: "margin-top:12px" }, check, save), error, result);
}

// Which settings, data and code produced a research result, so it can be reproduced or compared (runs.py).
function runLine(run) {
  if (!run) return null;
  return el("p", { class: "small muted", title: `Settings ${run.config_hash} · data ${run.data_hash} · result ${run.result_hash}${run.seed !== null && run.seed !== undefined ? ` · seed ${run.seed}` : ""}` },
    `Run ${run.id ?? "(not saved)"} · app ${run.versions ? run.versions.app : ""} · settings ${run.config_hash.slice(0, 8)} · data ${run.data_hash.slice(0, 8)} · result ${run.result_hash.slice(0, 8)}. The same settings and data always give the same result.`);
}

function metric(value, label) { return el("div", { class: "metric" }, el("div", { class: "v" }, value), el("div", { class: "k", text: label })); }

/* ---------- view: research ---------- */
function buildResearch() {
  const sym = el("input", { class: "sym", type: "text", value: "AAPL", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const go = el("button", { class: "primary", text: "Build the brief" });
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const run = () => withBusy(go, error, async () => { out.replaceChildren(renderResearch(await api(`/api/research/${encodeURIComponent(sym.value.trim())}`))); });
  go.addEventListener("click", run);
  sym.addEventListener("keydown", e => { if (e.key === "Enter") run(); });
  return el("section", {}, el("h2", { text: "Research a stock" }),
    el("p", { class: "lede", text: "The facts worth knowing before you look at an option chain: where the price sits, how volatile it is, what the market is doing, when earnings are due, and what the company has filed." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Ticker", sym), go), error), out);
}

function renderResearch(d) {
  const frag = el("div");
  for (const w of d.data_warnings) frag.append(banner("warn", w));
  if (d.last_bar && d.last_bar.status === "PROVISIONAL") frag.append(banner("info", d.last_bar.note));
  const q = d.quote, change = isNum(q.last) && isNum(q.prevclose) ? 100 * (q.last / q.prevclose - 1) : null;
  const top = el("div", { class: "panel" },
    el("div", { class: "price-head" }, el("span", { class: "name", text: `${d.symbol} ${d.description || ""}` }),
      el("span", { text: `${money(q.last)} (${signedPct(change)} today)` }), el("span", { class: "muted", text: `Daily bars through ${shortDate(d.as_of)}` })),
    el("h4", { text: "52-week range" }),
    el("div", { class: "range-bar" }, isNum(d.range.position) ? el("i", { style: `left:${Math.max(0, Math.min(100, d.range.position))}%` }) : null),
    el("div", { class: "row small muted", style: "justify-content:space-between" }, el("span", { text: `Low ${money(d.range.low)}` }),
      el("span", { text: isNum(d.range.position) ? `${num(d.range.position, 0)}% of the way up` : "" }), el("span", { text: `High ${money(d.range.high)}` })));
  const tech = techPanel(d.symbol);
  frag.append(top, tech.panel, tech.structure);

  const vol = d.volatility, rank = vol.iv_rank || {};
  const volPanel = el("div", { class: "panel" }, el("h3", { text: "Volatility" }),
    el("div", { class: "metrics" },
      metric(isNum(vol.realized_20d) ? pct(vol.realized_20d * 100, 0) : "n/a", "Actual, last 20 days"),
      metric(isNum(vol.implied_30d) ? pct(vol.implied_30d * 100, 0) : "n/a", "Implied, next 30 days"),
      metric(isNum(rank.rank) && rank.days >= 60 ? num(rank.rank, 0) : `${rank.days || 0} days`, isNum(rank.rank) && rank.days >= 60 ? "Implied volatility rank" : "of history recorded for rank")),
    el("p", { class: "small muted", text: "Implied volatility well above actual means options are priced for a bigger move than the stock has been making." }));
  const e = d.earnings;
  const regime = d.regime;
  const ctx = el("div", { class: "panel" }, el("h3", { text: "Market and calendar" }),
    regime.available ? el("p", {}, statusTag(regime.label === "uptrend" ? "pass" : regime.label === "downtrend" ? "fail" : "warn",
      regime.label === "uptrend" ? "Market uptrend" : regime.label === "downtrend" ? "Market downtrend" : "Market mixed"),
      ` ${regime.text} SPY is ${signedPct(regime.vs_200d_pct)} from its 200-day average and ${signedPct(regime.off_52w_high_pct)} from its 52-week high.` +
      (isNum(regime.vix) ? ` VIX ${num(regime.vix, 1)}.` : "")) : el("p", { class: "muted", text: regime.note }),
    el("p", {}, el("strong", { text: "Next earnings: " }), e ? `${shortDate(e.date)} (${e.source}). ${e.estimated ? "Confirm the date with the company before trading around it." : ""}` : "unknown. Look it up before buying options on this stock."));
  frag.append(el("div", { class: "two" }, volPanel, ctx));

  const trend = el("div", { class: "panel" }, el("h3", { text: d.trend.status === "PROVISIONAL" ? "Indicators (provisional: today's bar is still forming)" : "Indicators" }),
    table([{ label: "Indicator", cell: r => r.name }, { label: "Reading", cell: r => r.text },
      { label: "Leans", cell: r => r.lean === "neutral" ? "Neither way" : r.lean[0].toUpperCase() + r.lean.slice(1) }], d.trend.readings),
    el("h4", { text: "Patterns completed in the last ten trading days" }),
    d.recent_patterns.length ? el("ul", { class: "plain small" }, d.recent_patterns.map(p => el("li", {},
      `${p.label}, ${shortDate(p.date)} (${p.direction > 0 ? "bullish" : "bearish"} idea). `,
      p.status === "PROVISIONAL" ? el("span", {}, statusTag("warn", "Provisional"), " today's bar is not finished, so this can still disappear by the close. ") : null,
      el("span", { class: "muted", text: labVerdictText(p.verdicts) }))))
      : el("p", { class: "small muted", text: "None." }));
  frag.append(trend, dataChecksPanel(d.symbol));

  const co = d.company;
  const filings = el("div", { class: "panel" }, el("h3", { text: co.company ? `SEC filings: ${co.company.name}` : "SEC filings" }));
  for (const msg of co.errors) filings.append(el("p", { class: "small muted", text: msg }));
  if (co.available) {
    if (co.company.industry) filings.append(el("p", { class: "small muted", text: `Industry: ${co.company.industry}.` }));
    filings.append(table([
      { label: "Filed", cell: r => shortDate(r.filed) }, { label: "Form", cell: r => el("a", { href: r.url, target: "_blank", rel: "noopener", text: r.form }) },
      { label: "What it covers", cell: r => r.items.length ? r.items.map(i => i.label).join("; ") : (r.period ? `Period ending ${shortDate(r.period)}` : r.description) }], co.filings));
    const f = co.fundamentals;
    if (f && f.revenue.length) {
      const eps = Object.fromEntries(f.eps_diluted.map(r => [r.quarter, r])), ni = Object.fromEntries(f.net_income.map(r => [r.quarter, r]));
      filings.append(el("h4", { text: "Reported results by quarter" }), table([
        { label: "Quarter", cell: r => r.quarter },
        { label: "Revenue", num: true, cell: r => `${money(r.value / 1e9, 2)}B` },
        { label: "vs year before", num: true, cell: r => isNum(r.year_over_year) ? signedPct(r.year_over_year * 100) : "n/a" },
        { label: "Net income", num: true, cell: r => ni[r.quarter] ? `${money(ni[r.quarter].value / 1e9, 2)}B` : "n/a" },
        { label: "Diluted EPS", num: true, cell: r => eps[r.quarter] ? money(eps[r.quarter].value) : "n/a" }], f.revenue),
        el("p", { class: "small muted", text: f.note }));
    }
  }
  filings.append(el("h4", { text: "Not in this brief" }), el("ul", { class: "plain small muted" }, d.not_included.map(t => el("li", { text: t }))));
  frag.append(filings);
  return frag;
}

// What was found when the daily bars were checked: unfinished bar, gaps, bad prices, possible splits.
const CHECK_LEVEL = { info: "info", warn: "warn", fail: "fail" };
function dataChecksPanel(symbol) {
  const box = el("div", { class: "panel" }, el("h3", { text: "Data checks" }), el("p", { class: "small muted", text: "Checking the daily bars…" }));
  api(`/v2/data/${encodeURIComponent(symbol)}`).then(q => {
    box.replaceChildren(el("h3", { text: "Data checks" }),
      el("p", { class: "small muted", text: `${num(q.bars, 0)} daily bars, ${shortDate(q.first_date)} to ${shortDate(q.last_date)}; the last finished one is ${shortDate(q.last_complete_date)}. Fetched ${q.fetched_at ? q.fetched_at.replace("T", " ").slice(0, 16) : "earlier"} New York time.` }),
      el("ul", { class: "plain small" }, q.checks.map(c => el("li", {}, statusTag(CHECK_LEVEL[c.level] || "info", c.level === "info" ? "Note" : c.level === "warn" ? "Caution" : "Problem"), ` ${c.text}`))));
  }).catch(e => box.replaceChildren(el("h3", { text: "Data checks" }), el("p", { class: "small error", text: e.message })));
  return box;
}

let uid = 0;
/* ---------- shared chart helpers ---------- */
function niceScale(lo, hi, count = 5) {
  if (!(hi > lo)) { const pad = Math.abs(hi) * 0.05 || 1; lo -= pad; hi += pad; }
  const rough = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= rough);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
  return { lo: a, hi: b, step, ticks };
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = iso => `${MONTHS[+iso.slice(5, 7) - 1]} ’${iso.slice(2, 4)}`;
const halo = attrs => svg("text", { ...attrs, class: `${attrs.class || ""} halo` });
// Right-hand labels pushed apart so they never overlap.
function spreadLabels(items, gap = 13, top = -Infinity, bottom = Infinity) {
  items.sort((a, b) => a.y - b.y);
  for (let i = 1; i < items.length; i++) if (items[i].y - items[i - 1].y < gap) items[i].y = items[i - 1].y + gap;
  const overflow = items.length ? items[items.length - 1].y - bottom : 0;
  if (overflow > 0) for (const it of items) it.y -= overflow;
  for (let i = 0; i < items.length; i++) if (items[i].y < top + i * gap) items[i].y = top + i * gap;
  return items;
}
function tooltipAt(tip, box, xView, W) {
  tip.style.display = "block";
  const left = xView * box.width / W;
  tip.style.left = `${left > box.width * 0.6 ? left - tip.offsetWidth - 14 : left + 14}px`;
  tip.style.top = "8px";
}
const tipRow = (value, name, color) => el("div", { class: "tip-row" },
  color ? el("i", { style: `background: var(${color})` }) : el("i", { class: "blank" }), el("strong", { text: value }), ` ${name}`);

/* ---------- technical chart: price with averages, levels, swings, trendlines; MACD below ---------- */
const TECH_LINES = [
  { key: "ema20", name: "20-day", color: "--series-4" },
  { key: "sma50", name: "50-day", color: "--series-2" },
  { key: "sma200", name: "200-day", color: "--series-3" },
];
const STATE_COLORS = { 1: "--div-pos", "-1": "--div-neg", 0: "--div-mid" };
const STATE_WORDS = { 1: "Higher highs and higher lows", "-1": "Lower highs and lower lows", 0: "Mixed swings" };

function techChart(t, opt) {
  const rows = t.bars.slice(-opt.bars), n = rows.length, show = opt.show;
  const wrap = el("div", { class: "chart" });
  if (n < 2) { wrap.append(el("p", { class: "muted", text: "Not enough data to draw." })); return wrap; }
  const W = 760, pad = { l: 58, r: 104 };
  const priceTop = 12, priceBot = 312, ribTop = 320, ribBot = 327;
  const macdTop = 356, macdBot = 456;
  const axisY = (show.macd ? macdBot : ribBot) + 16, H = axisY + 8;
  const L = pad.l, R = W - pad.r;
  const X = i => L + (R - L) * i / (n - 1);
  const index = new Map(rows.map((r, i) => [r.date, i]));
  const offset = t.bars.length - n;           // position of the first visible bar in the full history

  let lo = Infinity, hi = -Infinity;
  for (const r of rows) {
    lo = Math.min(lo, r.low); hi = Math.max(hi, r.high);
    for (const s of TECH_LINES) if (show[s.key] && isNum(r[s.key])) { lo = Math.min(lo, r[s.key]); hi = Math.max(hi, r[s.key]); }
  }
  const ps = niceScale(lo, hi, 6);
  const Y = v => priceTop + (priceBot - priceTop) * (1 - (v - ps.lo) / (ps.hi - ps.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${t.symbol} daily price chart, ${shortDate(rows[0].date)} to ${shortDate(rows[n - 1].date)}` });
  const clipId = `clip${++uid}`;
  const defs = svg("defs", {}, svg("clipPath", { id: clipId }, svg("rect", { x: L - 4, y: priceTop, width: R - L + 8, height: priceBot - priceTop })));
  root.append(defs);
  for (const v of ps.ticks) {
    root.append(svg("line", { class: "grid", x1: L, x2: R, y1: Y(v), y2: Y(v) }),
      svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, ps.step < 1 ? 2 : 0) }));
  }
  const months = [];
  rows.forEach((r, i) => { if (i === 0 || r.date.slice(5, 7) !== rows[i - 1].date.slice(5, 7)) months.push(i); });
  const every = Math.max(1, Math.ceil(months.length / 7));
  months.filter((_, k) => k % every === 0).forEach(i => {
    if (i > 0 && X(i) < R - 20) root.append(svg("text", { x: X(i), y: axisY, "text-anchor": "middle", text: monthLabel(rows[i].date) }));
  });
  const plot = svg("g", { "clip-path": `url(#${clipId})` });
  root.append(plot);

  // Price: candles when they are wide enough to read, a closing line otherwise.
  const step = (R - L) / (n - 1), candles = step >= 3;
  // Today's bar so far is drawn dashed: it is not finished and can still change shape by the close.
  const forming = !!(t.last_bar && t.last_bar.status === "PROVISIONAL");
  if (candles) {
    const bw = Math.max(1.5, Math.min(9, step * 0.62));
    for (const [i, r] of rows.entries()) {
      const up = r.close >= r.open, top = Y(Math.max(r.open, r.close)), h = Math.max(1, Math.abs(Y(r.open) - Y(r.close)));
      const open = forming && i === n - 1 ? " forming" : "";
      plot.append(svg("line", { class: "wick" + open, x1: X(i), x2: X(i), y1: Y(r.high), y2: Y(r.low) }),
        svg("rect", { class: (up ? "candle up" : "candle down") + open, x: X(i) - bw / 2, y: top, width: bw, height: h }));
    }
  } else {
    plot.append(svg("path", { class: "close-line", d: rows.map((r, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(r.close).toFixed(1)}`).join(" ") }));
  }
  const ends = [];
  for (const s of TECH_LINES) {
    if (!show[s.key]) continue;
    let d = "", pen = false, last = null;
    rows.forEach((r, i) => {
      if (!isNum(r[s.key])) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)} ${Y(r[s.key]).toFixed(1)} `; pen = true; last = r[s.key];
    });
    if (d) { plot.append(svg("path", { d, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2, "stroke-linejoin": "round" })); }
    if (last !== null) ends.push({ text: s.name, y: Y(last) + 4 });
  }
  if (!candles) ends.push({ text: "Close", y: Y(rows[n - 1].close) + 4 });

  // Support and resistance: dashed, from the first swing that formed them. Their labels, like the
  // averages', sit in the right margin, spread apart so none overlaps another or the price.
  if (show.levels) {
    const levels = [...t.levels.resistance.map(l => ({ ...l, side: "Resistance" })), ...t.levels.support.map(l => ({ ...l, side: "Support" }))];
    for (const lv of levels) {
      if (lv.price < ps.lo || lv.price > ps.hi) continue;
      const i0 = index.has(lv.first_date) ? index.get(lv.first_date) : 0;
      plot.append(svg("line", { class: "level", x1: X(i0), x2: R, y1: Y(lv.price), y2: Y(lv.price) }));
      ends.push({ y: Y(lv.price) + 4, text: `${lv.side[0]} ${money(lv.price)}${lv.touches > 1 ? ` ×${lv.touches}` : ""}`, cls: "lbl" });
    }
  }
  // Trendlines through the last two rising lows or falling highs, extended to today.
  if (show.trend) {
    for (const tl of t.trendlines) {
      const toRow = e => e - (tl.to_index - (n - 1));
      const valueAt = row => tl.through_price + tl.slope_per_day * (row + (tl.to_index - (n - 1)) - tl.through_index);
      const r0 = Math.max(0, toRow(tl.from_index));
      plot.append(svg("line", { class: "trendline", x1: X(r0), y1: Y(valueAt(r0)), x2: R, y2: Y(valueAt(n - 1)) }));
      if (tl.value_now >= ps.lo && tl.value_now <= ps.hi) ends.push({ y: Y(tl.value_now) + 4, text: `Trend ${money(tl.value_now)}`, cls: "lbl accent" });
    }
  }
  // Swing points and their labels.
  if (show.swings) {
    for (const s of t.swings) {
      const i = index.get(s.date);
      if (i === undefined) continue;
      const y = Y(s.price), isHigh = s.kind === "high";
      plot.append(svg("circle", { class: "swing-dot", cx: X(i), cy: y, r: 3 }));
      root.append(halo({ x: X(i), y: isHigh ? y - 8 : y + 16, "text-anchor": "middle", class: "swing-lbl", text: s.label || (isHigh ? "H" : "L") }));
    }
  }
  for (const e of spreadLabels(ends, 13, priceTop + 4, priceBot)) root.append(svg("text", { x: R + 8, y: e.y, class: e.cls || null, text: e.text }));

  // Structure ribbon: one strip coloured by the trend state on each day.
  root.append(svg("text", { x: L - 8, y: ribBot, "text-anchor": "end", text: "Trend" }));
  let runStart = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || rows[i].state !== rows[runStart].state) {
      const x0 = runStart === 0 ? L : (X(runStart - 1) + X(runStart)) / 2, x1 = i === n ? R : (X(i - 1) + X(i)) / 2;
      root.append(svg("rect", { x: x0, y: ribTop, width: Math.max(0.5, x1 - x0), height: ribBot - ribTop, fill: `var(${STATE_COLORS[rows[runStart].state]})` }));
      runStart = i;
    }
  }

  // MACD panel on its own scale below the price.
  let MY = null;
  if (show.macd) {
    const vals = rows.flatMap(r => [r.macd, r.macd_signal, r.macd_hist]).filter(isNum);
    const ms = niceScale(Math.min(0, ...vals), Math.max(0, ...vals), 3);
    MY = v => macdTop + (macdBot - macdTop) * (1 - (v - ms.lo) / (ms.hi - ms.lo));
    root.append(svg("text", { x: L, y: macdTop - 8, class: "panel-title", text: "MACD (12, 26, 9)" }));
    for (const v of ms.ticks) {
      root.append(svg("line", { class: v === 0 ? "zero" : "grid", x1: L, x2: R, y1: MY(v), y2: MY(v) }),
        svg("text", { x: L - 8, y: MY(v) + 4, "text-anchor": "end", text: num(v, ms.step < 1 ? (ms.step < 0.1 ? 2 : 1) : 0) }));
    }
    const bw = Math.max(1, Math.min(7, step * 0.6));
    rows.forEach((r, i) => {
      if (!isNum(r.macd_hist)) return;
      const y0 = MY(0), y1 = MY(r.macd_hist);
      root.append(svg("rect", { class: "hist", x: X(i) - bw / 2, y: Math.min(y0, y1), width: bw, height: Math.max(0.5, Math.abs(y1 - y0)) }));
    });
    const mEnds = [];
    for (const [key, name, color] of [["macd", "MACD", "--series-1"], ["macd_signal", "Signal", "--series-2"]]) {
      let d = "", pen = false, last = null;
      rows.forEach((r, i) => { if (!isNum(r[key])) { pen = false; return; } d += `${pen ? "L" : "M"}${X(i).toFixed(1)} ${MY(r[key]).toFixed(1)} `; pen = true; last = r[key]; });
      root.append(svg("path", { d, fill: "none", stroke: `var(${color})`, "stroke-width": 2, "stroke-linejoin": "round" }));
      if (last !== null) mEnds.push({ text: name, y: MY(last) + 4 });
    }
    for (const e of spreadLabels(mEnds, 13, macdTop + 4, macdBot)) root.append(svg("text", { x: R + 8, y: e.y, text: e.text }));
  }

  // Crosshair and one tooltip for every series on the day under the pointer.
  const hover = svg("line", { class: "hover-line", y1: priceTop, y2: show.macd ? macdBot : ribBot, visibility: "hidden" });
  root.append(hover);
  const tip = el("div", { class: "tip" });
  const fmt = v => isNum(v) ? money(v) : "n/a";
  const move = evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - L) / (R - L) * (n - 1))));
    const r = rows[i];
    hover.setAttribute("x1", X(i)); hover.setAttribute("x2", X(i)); hover.setAttribute("visibility", "visible");
    const change = i > 0 ? 100 * (r.close / rows[i - 1].close - 1) : null;
    tip.replaceChildren(el("div", { class: "tip-head", text: shortDate(r.date) }),
      tipRow(`${fmt(r.close)}${isNum(change) ? ` (${signedPct(change)})` : ""}`, "close"),
      el("div", { class: "tip-sub", text: `Open ${fmt(r.open)}  High ${fmt(r.high)}  Low ${fmt(r.low)}` }),
      ...TECH_LINES.filter(s => show[s.key]).map(s => tipRow(fmt(r[s.key]), s.name, s.color)),
      ...(show.macd ? [tipRow(num(r.macd), "MACD", "--series-1"), tipRow(num(r.macd_signal), "signal", "--series-2"),
        tipRow(num(r.macd_hist), "histogram")] : []),
      el("div", { class: "tip-sub", text: STATE_WORDS[r.state] }));
    tooltipAt(tip, box, X(i), W);
  };
  root.addEventListener("pointermove", move);
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.style.display = "none"; });

  const legend = el("div", { class: "legend" },
    candles ? el("span", {}, el("b", { class: "key-candle up" }), el("b", { class: "key-candle down" }), "Candles: hollow closed up, filled closed down")
      : el("span", {}, el("i", { style: "background: var(--series-1)" }), "Close"),
    ...TECH_LINES.filter(s => show[s.key]).map(s => el("span", {}, el("i", { style: `background: var(${s.color})` }), s.name + " average")),
    show.levels ? el("span", {}, el("i", { class: "key-dash" }), "R resistance, S support (×2: two swings formed it)") : null,
    show.trend && t.trendlines.length ? el("span", {}, el("i", { style: "background: var(--accent)" }), "Trendline") : null,
    forming && candles ? el("span", {}, el("b", { class: "key-candle forming-key" }), "Today so far (not finished)") : null,
    el("span", {}, "Trend strip: ", el("b", { class: "key-box", style: "background: var(--div-pos)" }), "up ",
      el("b", { class: "key-box", style: "background: var(--div-neg)" }), "down ", el("b", { class: "key-box", style: "background: var(--div-mid)" }), "mixed"));
  wrap.append(el("div", { class: "chart-scroll" }, root, tip), legend);
  return wrap;
}

function techPanel(symbol) {
  const range = segmented(`trange${++uid}`, [["63", "3M"], ["126", "6M"], ["252", "1Y"], ["504", "2Y"]], "126");
  const show = { ema20: false, sma50: true, sma200: true, levels: true, swings: true, trend: true, macd: true };
  const toggleNames = [["ema20", "20-day"], ["sma50", "50-day"], ["sma200", "200-day"], ["levels", "Support & resistance"],
    ["swings", "Swing labels"], ["trend", "Trendlines"], ["macd", "MACD"]];
  const size = el("select", {}, [["1.5", "Small (1.5 ATR)"], ["2", "Medium (2 ATR)"], ["3", "Large (3 ATR)"]].map(([v, t]) => el("option", { value: v, text: t, selected: v === "2" })));
  const chartBox = el("div", { class: "chart-box" }, el("p", { class: "muted", text: "Loading chart…" }));
  const structBox = el("div", { class: "panel" }, el("h3", { text: "Price structure" }), el("p", { class: "muted", text: "Loading…" }));
  let data = null, request = 0;
  const draw = () => {
    if (!data) return;
    chartBox.replaceChildren(techChart(data, { bars: Number(range.value()), show }));
    // Where the chart scrolls (a phone), start at the latest day.
    requestAnimationFrame(() => chartBox.querySelectorAll(".chart-scroll").forEach(sc => { sc.scrollLeft = sc.scrollWidth; }));
  };
  const boxes = toggleNames.map(([key, label]) => {
    const box = el("input", { type: "checkbox", checked: show[key] });
    box.addEventListener("change", () => { show[key] = box.checked; draw(); });
    return el("label", { class: "check" }, box, label);
  });
  async function load() {
    const mine = ++request;
    if (data) chartBox.classList.add("refetching");
    try {
      const got = await api(`/api/technicals/${encodeURIComponent(symbol)}?bars=504&swing_atr=${size.value}`);
      if (mine !== request) return;              // the swing size was changed again meanwhile
      data = got;
      draw();
      structBox.replaceChildren(...structureView(data));
    } catch (e) {
      if (mine !== request) return;
      chartBox.replaceChildren(el("p", { class: "error", text: e.message }));
      structBox.replaceChildren(el("h3", { text: "Price structure" }), el("p", { class: "error", text: e.message }));
    } finally { if (mine === request) chartBox.classList.remove("refetching"); }
  }
  range.addEventListener("change", draw);
  size.addEventListener("change", load);
  load();
  const panel = el("div", { class: "panel" },
    el("h3", { text: "Chart" }),
    el("div", { class: "row chart-controls" }, field("Range", range), field("Swing size", size), el("div", { class: "toggles" }, boxes)),
    chartBox,
    el("p", { class: "small muted", text: "Swings are moves of at least the chosen number of average true ranges (ATR, the stock's typical daily range), so a quiet stock needs a smaller move than a volatile one. A swing is labelled only once the move away from it has happened, which is some days after the high or low itself." }));
  return { panel, structure: structBox };
}

function structureView(t) {
  const s = t.structure, out = [el("h3", { text: "Price structure" })];
  const tag = s.state === "up" ? statusTag("pass", "Uptrend") : s.state === "down" ? statusTag("fail", "Downtrend") : statusTag("warn", "No clear trend");
  const what = s.headline.split(": ").slice(1).join(": ") || s.headline;
  out.push(el("p", {}, tag, ` ${what[0].toUpperCase()}${what.slice(1)} (since ${shortDate(s.state_since)}).`),
    el("p", { class: "small", text: s.detail }), s.watch ? el("p", { class: "small", text: s.watch }) : null);
  if (t.structure_status === "PROVISIONAL" && t.confirmed_structure) {
    const c = t.confirmed_structure, same = c.state === s.state && c.headline === s.headline;
    out.push(el("p", { class: "small" }, statusTag("warn", "Provisional"),
      same ? ` This includes today's unfinished bar; on finished bars alone (through ${shortDate(t.last_bar.last_complete_date)}) the reading is the same.`
        : ` This includes today's unfinished bar. On finished bars alone (through ${shortDate(t.last_bar.last_complete_date)}): ${c.headline}`));
  }
  const rows = [...t.levels.resistance.slice().reverse().map(l => ({ ...l, side: "Resistance" })), ...t.levels.support.map(l => ({ ...l, side: "Support" }))];
  if (rows.length) {
    out.push(el("h4", { text: "Support and resistance" }), table([
      { label: "Level", cell: r => r.side },
      { label: "Price", num: true, cell: r => money(r.price) },
      { label: "From here", num: true, cell: r => signedPct(r.distance_pct) },
      { label: "Swings at it", num: true, cell: r => `${r.touches} (${r.strength})` },
      { label: "Last touched", cell: r => shortDate(r.last_date) }], rows));
  }
  if (!t.levels.resistance.length) out.push(el("p", { class: "small muted", text: "No resistance: the price is above every swing high of the last year." }));
  if (!t.levels.support.length) out.push(el("p", { class: "small muted", text: "No support: the price is below every swing low of the last year." }));
  for (const tl of t.trendlines) {
    const name = tl.kind === "rising support" ? "Rising support line" : "Falling resistance line";
    const crossed = tl.kind === "rising support" ? "The price has closed below it." : "The price has closed above it.";
    out.push(el("p", { class: "small", text: `${name} through the swings of ${shortDate(tl.from_date)} and ${shortDate(tl.through_date)}: ${money(tl.value_now)} today. ${tl.crossed ? crossed : ""}` }));
  }
  const names = { structure_up: "Swings turn to higher highs and higher lows", structure_down: "Swings turn to lower highs and lower lows",
    structure_up_break: "Uptrend breaks below the last higher low", structure_down_break: "Downtrend breaks above the last lower high" };
  out.push(el("h4", { text: "Has this worked before? (pattern lab)" }),
    el("ul", { class: "plain small" }, Object.entries(names).map(([k, label]) => el("li", {}, `${label}: `, el("span", { class: "muted", text: labVerdictText(t.lab[k]) })))),
    el("p", { class: "small muted", text: "The pattern lab tests these four structure events on your watchlist with the medium swing size. Run it from the Pattern lab screen to get verdicts." }));
  return out.filter(Boolean);
}

/* ---------- view: option simulator ---------- */
function pnlChart(s) {
  const W = 760, H = 320, pad = { l: 72, r: 108, t: 22, b: 34 };
  const xs = s.curve.prices, a = s.curve.pnl_at_date, b = s.curve.pnl_at_expiry, n = xs.length;
  const sameDay = s.eval_date === s.first_expiration;
  const wrap = el("div", { class: "chart" });
  const ys = niceScale(Math.min(0, ...a, ...b), Math.max(0, ...a, ...b), 5);
  const xlo = xs[0], xhi = xs[n - 1];
  const X = p => pad.l + (W - pad.l - pad.r) * (p - xlo) / (xhi - xlo);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Profit and loss of the position against the stock price` });
  const sd = s.odds_at_expiry.one_sd_move;
  if (sd > 0) {
    const x0 = X(Math.max(xlo, s.spot - sd)), x1 = X(Math.min(xhi, s.spot + sd));
    root.append(svg("rect", { class: "band", x: x0, y: pad.t, width: Math.max(0, x1 - x0), height: H - pad.t - pad.b }),
      svg("text", { x: (x0 + x1) / 2, y: pad.t - 8, "text-anchor": "middle", text: "Typical range by expiration (±1 standard deviation)" }));
  }
  for (const v of ys.ticks) {
    root.append(svg("line", { class: v === 0 ? "zero" : "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
      svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, 0) }));
  }
  const xt = niceScale(xlo, xhi, 6);
  for (const v of xt.ticks) if (v >= xlo && v <= xhi) root.append(svg("text", { x: X(v), y: H - 12, "text-anchor": "middle", text: money(v, xt.step < 1 ? 2 : 0) }));
  root.append(svg("text", { x: (pad.l + W - pad.r) / 2, y: H, "text-anchor": "middle", text: "Stock price" }));
  root.append(svg("line", { class: "now-line", x1: X(s.spot), x2: X(s.spot), y1: pad.t, y2: H - pad.b }),
    halo({ x: X(s.spot) + 5, y: pad.t + 12, class: "lbl", text: `Now ${money(s.spot)}` }));
  const series = sameDay ? [{ ys: b, name: "At expiration", color: "--series-2" }]
    : [{ ys: a, name: `On ${shortDate(s.eval_date)}`, color: "--series-1" }, { ys: b, name: "At expiration", color: "--series-2" }];
  const ends = [];
  for (const sr of series) {
    root.append(svg("path", { d: sr.ys.map((v, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)} ${Y(v).toFixed(1)}`).join(" "),
      fill: "none", stroke: `var(${sr.color})`, "stroke-width": 2, "stroke-linejoin": "round" }));
    ends.push({ text: sr.name, y: Y(sr.ys[n - 1]) + 4 });
  }
  for (const e of spreadLabels(ends, 13, pad.t + 4, H - pad.b)) root.append(svg("text", { x: W - pad.r + 8, y: e.y, text: e.text }));
  for (const be of s.expiry.breakevens) {
    if (be < xlo || be > xhi) continue;
    root.append(svg("circle", { class: "be-dot", cx: X(be), cy: Y(0), r: 4 }),
      halo({ x: X(be), y: Y(0) + 18, "text-anchor": "middle", class: "lbl", text: `Breakeven ${money(be)}` }));
  }
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
  const dots = series.map(sr => svg("circle", { r: 4, fill: `var(${sr.color})`, stroke: "var(--panel)", "stroke-width": 2, visibility: "hidden" }));
  root.append(hover, ...dots);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / (W - pad.l - pad.r) * (n - 1))));
    hover.setAttribute("x1", X(xs[i])); hover.setAttribute("x2", X(xs[i])); hover.setAttribute("visibility", "visible");
    series.forEach((sr, k) => { dots[k].setAttribute("cx", X(xs[i])); dots[k].setAttribute("cy", Y(sr.ys[i])); dots[k].setAttribute("visibility", "visible"); });
    tip.replaceChildren(el("div", { class: "tip-head", text: `Stock ${money(xs[i])} (${signedPct(100 * (xs[i] / s.spot - 1))} from now)` }),
      ...series.map(sr => tipRow(money(sr.ys[i], 0), sr.name.toLowerCase(), sr.color)));
    tooltipAt(tip, box, X(xs[i]), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); tip.style.display = "none"; });
  wrap.append(el("div", { class: "chart-scroll" }, root, tip), el("div", { class: "legend" }, series.map(sr => el("span", {}, el("i", { style: `background: var(${sr.color})` }), sr.name)),
    el("span", {}, el("b", { class: "key-box band-key" }), "Typical range by expiration")));
  return wrap;
}

function pnlGrid(s) {
  const g = s.grid, max = Math.max(1, ...g.pnl.flat().map(v => Math.abs(v)));
  const cost = Math.abs(s.net_premium);
  const head = el("tr", {}, el("th", { text: "Stock price" }), g.dates.map(d => el("th", { class: "num",
    text: d === g.dates[0] ? "Today" : d === s.first_expiration ? `${shortDate(d)} (expires)` : shortDate(d) })));
  const body = g.prices.map((p, r) => {
    const now = Math.abs(p - s.spot) < 0.006;
    return el("tr", { class: now ? "now-row" : null }, el("td", { class: "num", text: `${money(p)}${now ? " now" : ""}` }),
      g.pnl[r].map(v => {
        const share = Math.min(1, Math.abs(v) / max), mix = Math.round(10 + 55 * share);
        const bg = Math.abs(v) < 0.5 ? "var(--div-mid)" : `color-mix(in oklab, var(${v > 0 ? "--div-pos" : "--div-neg"}) ${mix}%, var(--div-mid))`;
        return el("td", { class: "num cell", style: `background: ${bg}`, title: cost ? `${money(v)} (${signedPct(100 * v / cost, 0)} of the premium)` : money(v), text: money(v, 0) });
      }));
  });
  return el("div", { class: "scroll" }, el("table", { class: "pnl-grid" }, el("thead", {}, head), el("tbody", {}, body)));
}

function renderSimulation(s) {
  const frag = el("div");
  if (s.data.mode === "demo") frag.append(banner("fail", "Demo data: these prices are made up."));
  const e = s.expiry, debit = s.net_premium >= 0;
  const gain = e.unlimited_gain ? "No limit" : money(e.max_gain, 0), loss = e.unlimited_loss ? "No limit" : money(e.max_loss, 0);
  const ga = s.greeks;
  frag.append(el("div", { class: "metrics sim-metrics" },
    metric(money(Math.abs(s.net_premium), 0), debit ? "You pay (debit)" : "You receive (credit)"),
    metric(loss, `Most you can lose${e.exact ? "" : " (approx.)"}`),
    metric(gain, `Most you can make${e.exact ? "" : " (approx.)"}`),
    metric(e.breakevens.length ? e.breakevens.map(b => money(b)).join(" and ") : "None", "Breakeven at expiration"),
    metric(pct(100 * s.odds_at_expiry.p_profit, 0), "Chance of profit at expiration"),
    metric(`${num(ga.delta, 0)} sh`, "Delta today: moves like this many shares"),
    metric(`${money(ga.theta, 2)}`, "Theta today: change in value per day"),
    metric(`${money(ga.vega, 2)}`, "Vega today: change per 1 volatility point")));
  frag.append(pnlChart(s));
  const oe = s.odds_at_expiry, od = s.odds_at_date;
  const dateWords = s.eval_date === s.first_expiration ? "at expiration" : s.days_to_eval === 0 ? "today" : `on ${shortDate(s.eval_date)}`;
  frag.append(el("p", { class: "small", text:
    `Option prices imply a ${pct(100 * oe.p_profit, 0)} chance this position is profitable at expiration` +
    (s.eval_date !== s.first_expiration && s.days_to_eval > 0 ? ` and ${pct(100 * od.p_profit, 0)} ${dateWords}` : "") + ". " +
    (isNum(oe.p_max_loss) && !e.unlimited_loss ? `They imply a ${pct(100 * oe.p_max_loss, 0)} chance of the full loss at expiration. ` : "") +
    `In 8 of 10 of the outcomes they price in, the stock ends between ${money(oe.low)} and ${money(oe.high)} at expiration, where this position would make ${money(oe.pnl_low, 0)} and ${money(oe.pnl_high, 0)}.` }));
  frag.append(el("h4", { text: "Profit and loss by stock price and date" }), pnlGrid(s),
    el("p", { class: "small muted", text: `With volatility ${s.iv_shift === 0 ? "as it is today" : `${s.iv_shift > 0 ? "up" : "down"} ${num(Math.abs(s.iv_shift * 100), 0)} points`}. Blue is profit and red is loss; hover a cell for the change as a share of the premium.` }));
  frag.append(el("h4", { text: "Legs" }), table([
    { label: "Leg", cell: l => l.description },
    { label: "Price used", num: true, cell: l => `${money(l.entry_price)} (${l.entry_source === "you" ? "your price" : "midpoint"})` },
    { label: "Bid / ask now", num: true, cell: l => `${money(l.bid)} / ${money(l.ask)}` },
    { label: "Volatility used", num: true, cell: l => pct(100 * l.vol, 1) }], s.legs));
  frag.append(el("h4", { text: "What this assumes" }), el("ul", { class: "plain small muted" }, s.notes.map(t => el("li", { text: t }))));
  return frag;
}

function buildSimulate() {
  const sym = el("input", { class: "sym", type: "text", value: "AAPL", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const legsBox = el("div", { class: "legs" });
  const add = el("button", { class: "plain", type: "button", text: "Add a leg" });
  const go = el("button", { class: "primary", text: "Simulate" });
  const note = el("div", { class: "loadnote small muted", "aria-live": "polite" });
  const error = el("div", { class: "error", role: "alert" });
  const dateSlider = el("input", { type: "range", min: 0, max: 0, step: 1, value: 0, "aria-label": "Value the position on" });
  const dateText = el("span", { class: "slider-value" });
  const ivSlider = el("input", { type: "range", min: -30, max: 30, step: 1, value: 0, "aria-label": "Change in implied volatility, in points" });
  const ivText = el("span", { class: "slider-value", text: "No change" });
  const rangeSel = el("select", { "aria-label": "Stock price range" }, [["", "Automatic"], ["10", "±10%"], ["20", "±20%"], ["30", "±30%"], ["50", "±50%"]].map(([v, t]) => el("option", { value: v, text: t })));
  const controls = el("div", { class: "row sim-controls hidden" },
    el("div", { class: "field grow" }, el("span", { text: "Value the position on" }), el("div", { class: "slider" }, dateSlider, dateText)),
    el("div", { class: "field grow" }, el("span", { text: "Implied volatility change" }), el("div", { class: "slider" }, ivSlider, ivText)),
    field("Stock price range", rangeSel));
  const out = el("div");
  let loadedFor = "", expirations = [], last = null, ticket = 0, timer = null;
  const chains = new Map();
  const legs = [];
  // A position always keeps one leg, and has at most four.
  const syncLegs = () => {
    add.disabled = legs.length >= 4;
    legs.forEach(l => l.node.querySelector(".remove").classList.toggle("hidden", legs.length === 1));
  };

  // Returns true when a new ticker was loaded (so the legs need refilling).
  async function loadSymbol() {
    const symbol = sym.value.trim().toUpperCase();
    if (!symbol) throw new Error("Enter a ticker.");
    if (symbol === loadedFor) return false;
    note.replaceChildren(el("span", { class: "spinner" }), el("span", { text: `Loading ${symbol} expirations…` }));
    try {
      const data = await api(`/api/expirations?symbol=${encodeURIComponent(symbol)}`);
      if (sym.value.trim().toUpperCase() !== symbol) return false;      // the ticker changed while this was on its way
      loadedFor = data.symbol || symbol; sym.value = loadedFor;
      expirations = data.expirations; chains.clear(); last = null;
      controls.classList.add("hidden"); out.replaceChildren();
      return true;
    } finally { note.replaceChildren(); }
  }
  async function chainFor(exp) {
    const key = `${loadedFor} ${exp}`;
    if (!chains.has(key)) chains.set(key, api(`/api/chain?symbol=${encodeURIComponent(loadedFor)}&expiration=${exp}&width_pct=1000`));
    try { return await chains.get(key); } catch (e) { chains.delete(key); throw e; }
  }

  function makeLeg(init = {}) {
    const id = ++uid;
    const side = segmented(`sside${id}`, [["buy", "Buy"], ["sell", "Sell"]], init.side || "buy");
    const qty = el("input", { type: "number", min: 1, max: 1000, step: 1, value: init.contracts || 1, "aria-label": "Contracts" });
    const kind = segmented(`skind${id}`, [["call", "Call"], ["put", "Put"]], init.option_type || "call");
    const exp = el("select", { "aria-label": "Expiration" });
    const strike = el("select", { "aria-label": "Strike" }, el("option", { text: "Loading…" }));
    const entry = el("input", { type: "number", min: 0.01, step: 0.01, placeholder: "midpoint", value: isNum(init.entry_price) ? init.entry_price.toFixed(2) : "", "aria-label": "Price per share" });
    const remove = el("button", { class: "plain small-btn remove", type: "button", text: "Remove leg" });
    const node = el("div", { class: "leg row" }, field("Action", side), field("Contracts", qty), field("Type", kind),
      field("Expiration", exp), field("Strike", strike), field("Price (per share)", entry), remove);
    let filling = 0;
    async function fillStrikes(preferred) {
      const mine = ++filling;
      strike.replaceChildren(el("option", { text: "Loading…" })); strike.disabled = true;
      const chain = await chainFor(exp.value);
      if (mine !== filling) return;               // another expiration or type was picked meanwhile
      const rows = chain.contracts.filter(c => c.option_type === kind.value());
      strike.replaceChildren(...rows.map(c => el("option", { value: c.strike,
        text: `${num(c.strike, c.strike % 1 ? 2 : 0)}   ${c.bid == null && c.ask == null ? "no quote" : `bid ${num(c.bid)} / ask ${num(c.ask)}`}` })));
      const ks = rows.map(c => c.strike);
      strike.value = ks.includes(Number(preferred)) ? preferred : ks.reduce((p, k) => Math.abs(k - chain.spot) < Math.abs(p - chain.spot) ? k : p, ks[0]);
      strike.disabled = !ks.length;
    }
    async function refresh(preferExp, preferStrike) {
      exp.replaceChildren(...expirations.map(e => el("option", { value: e.date, text: `${shortDate(e.date)} (${e.dte} days)` })));
      const dates = expirations.map(e => e.date);
      exp.value = dates.includes(preferExp) ? preferExp : (expirations.find(e => e.dte >= 30) || expirations[expirations.length - 1]).date;
      await fillStrikes(preferStrike);
    }
    const reset = () => { entry.value = ""; entry.title = ""; delete entry.dataset.source; };
    entry.addEventListener("input", () => { entry.dataset.source = "you"; entry.title = ""; });
    if (isNum(init.entry_price)) entry.dataset.source = init.entry_source || "you";
    exp.addEventListener("change", () => { reset(); fillStrikes(strike.value).catch(e => { error.textContent = e.message; }); });
    kind.addEventListener("change", () => { reset(); fillStrikes(strike.value).catch(e => { error.textContent = e.message; }); });
    strike.addEventListener("change", reset);
    const leg = {
      node, refresh,
      value: () => {
        if (strike.disabled || !strike.value) throw new Error("Each leg needs an expiration and a strike.");
        const price = entry.value === "" ? null : Number(entry.value);
        if (price !== null && !(price > 0)) throw new Error("A price must be more than zero.");
        return { expiration: exp.value, option_type: kind.value(), strike: Number(strike.value), side: side.value(),
          contracts: Math.max(1, Math.round(Number(qty.value) || 1)), entry_price: price };
      },
      fillEntry: (price, source) => {
        if (entry.value !== "") return;
        entry.value = String(Math.round(price * 10000) / 10000); entry.dataset.source = source;
        entry.title = source === "midpoint" ? "The midpoint when simulated. Change it to the price you expect to trade at." : "";
      },
      source: () => entry.dataset.source || (entry.value === "" ? "midpoint" : "you"),
      reset,
    };
    remove.addEventListener("click", () => {
      if (legs.length === 1) return;
      legs.splice(legs.indexOf(leg), 1); node.remove(); syncLegs();
    });
    return leg;
  }
  async function addLeg(init = {}) {
    if (legs.length >= 4) return;
    const leg = makeLeg(init);
    legs.push(leg); legsBox.append(leg.node); syncLegs();
    if (expirations.length) await leg.refresh(init.expiration, init.strike);
  }

  const showControls = s => {
    controls.classList.remove("hidden");
    dateSlider.max = String(s.dates.length - 1);
    dateSlider.value = String(Math.max(0, s.dates.indexOf(s.eval_date)));
    describeDate();
  };
  function describeDate() {
    const dates = last ? last.dates : [];
    const d = dates[Number(dateSlider.value)];
    if (!d) { dateText.textContent = ""; return; }
    const days = Math.round((new Date(d) - new Date(dates[0])) / 86400000);
    dateText.textContent = d === dates[0] ? `${shortDate(d)} (today)` : d === last.first_expiration ? `${shortDate(d)} (expiration)` : `${shortDate(d)} (in ${days} days)`;
  }
  const describeIv = () => { const v = Number(ivSlider.value); ivText.textContent = v === 0 ? "No change" : `${v > 0 ? "+" : ""}${v} points`; };

  async function run(useSlider) {
    const mine = ++ticket;
    error.textContent = "";
    try {
      if (await loadSymbol()) for (const leg of legs) { leg.reset(); await leg.refresh(); }
      if (mine !== ticket || !loadedFor) return;
      const sent = legs.slice();
      const body = { symbol: loadedFor, legs: sent.map(l => l.value()), iv_shift: Number(ivSlider.value),
        eval_date: useSlider && last ? last.dates[Number(dateSlider.value)] : null, range_pct: rangeSel.value ? Number(rangeSel.value) : null };
      out.classList.add("refetching"); go.disabled = true;
      const s = await api("/api/simulate", body);
      if (mine !== ticket) return;
      if (sent.length !== legs.length || sent.some(l => !legs.includes(l))) return run(useSlider);   // legs changed meanwhile
      // Freeze the entry prices, so moving the date or volatility does not move the starting point.
      s.legs.forEach((l, i) => { sent[i].fillEntry(l.entry_price, l.entry_source); l.entry_source = sent[i].source(); });
      last = s;
      if (!useSlider) showControls(s); else describeDate();
      out.replaceChildren(renderSimulation(s));
      requestAnimationFrame(() => out.querySelectorAll(".chart-scroll").forEach(sc => { sc.scrollLeft = (sc.scrollWidth - sc.clientWidth) / 2; }));
    } catch (e) { if (mine === ticket) error.textContent = e.message; }
    finally { if (mine === ticket) { out.classList.remove("refetching"); go.disabled = false; } }
  }
  const soon = () => { clearTimeout(timer); timer = setTimeout(() => run(true), 160); };
  dateSlider.addEventListener("input", () => { describeDate(); soon(); });
  ivSlider.addEventListener("input", () => { describeIv(); soon(); });
  rangeSel.addEventListener("change", () => run(true));
  go.addEventListener("click", () => run(false));
  add.addEventListener("click", () => addLeg().catch(e => { error.textContent = e.message; }));
  async function setTicker() {
    error.textContent = "";
    try {
      if (await loadSymbol()) for (const leg of legs) { leg.reset(); await leg.refresh(); }
    } catch (e) { error.textContent = e.message; }
  }
  sym.addEventListener("change", setTicker);
  sym.addEventListener("keydown", e => { if (e.key === "Enter") setTicker(); });

  const view = el("section", {},
    el("h2", { text: "Simulate a trade" }),
    el("p", { class: "lede", text: "See what a position could be worth at different stock prices, on any day up to expiration, and if implied volatility rises or falls. Up to four legs, so spreads and straddles work too." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Ticker", sym)), legsBox,
      el("div", { class: "row" }, add, go), note, error),
    el("div", { class: "panel" }, controls, out));
  view.loadPosition = async (symbol, init) => {
    ++ticket;
    sym.value = symbol; loadedFor = ""; legs.splice(0); legsBox.replaceChildren(); out.replaceChildren();
    ivSlider.value = "0"; describeIv(); rangeSel.value = ""; controls.classList.add("hidden"); error.textContent = "";
    try {
      await loadSymbol();
      for (const leg of init) await addLeg(leg);
      await run(false);
    } catch (e) { error.textContent = e.message; }
  };
  const pending = state.pendingSim;
  state.pendingSim = null;
  if (pending) view.loadPosition(pending.symbol, pending.legs);
  else (async () => {
    try { await loadSymbol(); if (!legs.length) await addLeg(); } catch (e) { error.textContent = e.message; }
  })();
  return view;
}

/* ---------- view: pattern lab ---------- */
const VERDICTS = { supported: ["pass", "Supported"], contradicted: ["fail", "Wrong way"], no_evidence: ["info", "No evidence"], insufficient: ["warn", "Too few cases"] };

function buildPatterns() {
  const symbols = el("textarea", { "aria-label": "Symbols" });
  const years = el("input", { type: "number", min: 2, max: 20, step: 1, value: 5 });
  const horizons = el("input", { type: "text", value: "5, 10, 20" });
  const go = el("button", { class: "primary", text: "Run the study" });
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const waitForStatus = setInterval(() => { if (state.status) { symbols.value = state.status.watchlist.join(", "); clearInterval(waitForStatus); } }, 100);
  const body = () => ({ symbols: symbols.value.split(/[\s,]+/).filter(Boolean), years: Number(years.value),
    horizons: horizons.value.split(/[\s,]+/).filter(Boolean).map(Number) });
  go.addEventListener("click", () => withBusy(go, error, async () => { out.replaceChildren(renderStudy(await api("/api/patterns/run", body()))); }));
  api("/api/patterns").then(d => { if (d.last_run) out.replaceChildren(renderStudy(d.last_run)); }).catch(() => {});
  return el("section", {}, el("h2", { text: "Pattern lab" }),
    el("p", { class: "lede", text: "Every pattern is tested the same way: find each past occurrence, buy or short at the next open, and compare the result with what the other stocks in the run did over the same days. A pattern counts as supported only if it beats chance in the first 60% of the history after correcting for how many ideas were tested, does so again in the held-out final 40%, and makes money after costs. Use at least five symbols; fewer get numbers but no verdict." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Years of history", years), field("Holding periods to test (trading days)", horizons), go),
      el("div", { style: "margin-top:12px" }, field("Symbols", symbols)), error), out);
}

function renderStudy(s) {
  const frag = el("div");
  if (s.data_source === "demo") frag.append(banner("fail", "This run used made-up demo prices, which are random by construction. The verdicts say nothing about real markets."));
  if (s.verdicts_given === false) frag.append(banner("warn", `Fewer than five symbols, so there is no fair comparison group. The numbers below are measured against ${s.benchmark} and no verdicts are given.`));
  const versus = s.verdicts_given === false ? `vs ${s.benchmark}` : "vs the other stocks";
  const counts = {};
  for (const r of s.results) counts[r.verdict] = (counts[r.verdict] || 0) + 1;
  const chartBox = el("div");
  const symPick = el("select", {}, s.symbols.map(x => el("option", { value: x, text: x })));
  let current = null;
  async function drawChart() {
    if (!current) return;
    chartBox.replaceChildren(el("p", { class: "muted", text: "Loading chart…" }));
    try {
      const d = await api(`/api/chart/${encodeURIComponent(symPick.value)}?pattern=${current.pattern}`);
      const r = current;
      chartBox.replaceChildren(
        el("h4", { text: `${r.label}, held ${r.horizon} days` }),
        el("p", { class: "small" }, statusTag(VERDICTS[r.verdict][0], VERDICTS[r.verdict][1]), ` ${r.verdict_text}`),
        el("p", { class: "small muted", text: `${r.description} ${r.n_events} cases in all (${r.n_in_sample} for discovery, ${r.n_out_of_sample} held out); ` +
          `${isNum(r.hit_rate) ? pct(r.hit_rate * 100, 0) : "n/a"} made money after costs. Discovery t-statistic ${isNum(r.t_in_sample) ? num(r.t_in_sample) : "n/a"}, ` +
          `held-out t-statistic ${isNum(r.t_out_of_sample) ? num(r.t_out_of_sample) : "n/a"} (one-sided p ${isNum(r.p_out_of_sample) ? num(r.p_out_of_sample, 3) : "n/a"}).` }),
        el("h4", { text: `${d.events.length} occurrence${d.events.length === 1 ? "" : "s"} on ${d.symbol} in the last year` }),
        lineChart({ series: [{ name: "Close", color: "--series-1", points: d.bars.map(b => ({ x: b.date, y: b.close })) }],
          events: d.events.map(date => ({ date, direction: d.direction })), yFormat: v => money(v, 0) }),
        el("p", { class: "small muted", text: "Triangles mark the day each pattern completed. They point the way the pattern expects the price to go." }));
    } catch (e) { chartBox.replaceChildren(el("p", { class: "error", text: e.message })); }
  }
  symPick.addEventListener("change", drawChart);
  frag.append(el("div", { class: "panel" },
    el("h3", { text: `${s.tests_run} tests on ${s.symbols.length} symbols, ${shortDate(s.first_date)} to ${shortDate(s.last_date)}` }),
    el("p", { class: "small muted", text: `${counts.supported || 0} supported, ${counts.contradicted || 0} pointed the wrong way, ${counts.no_evidence || 0} showed no evidence, ${counts.insufficient || 0} had too few cases. Each stock is compared with ${s.benchmark}. "After costs" is the average return per trade; "${versus}" is how much better it did than the comparison; "Held-out" is the same for the final 40% of the history, which starts ${shortDate(s.split_date)}. Costs: ${s.cost_bps_per_side} basis points per side. Select a row for the reasoning and a chart.` }),
    table([
      { label: "Pattern", cell: r => r.label },
      { label: "Hold", num: true, cell: r => `${r.horizon} d` },
      { label: "Cases", num: true, cell: r => r.n_events },
      { label: "After costs", num: true, cell: r => isNum(r.mean_net) ? signedPct(r.mean_net * 100, 2) : "n/a" },
      { label: versus, num: true, cell: r => isNum(r.mean_excess) ? signedPct(r.mean_excess * 100, 2) : "n/a" },
      { label: "Held-out", num: true, cell: r => isNum(r.out_of_sample_excess) ? signedPct(r.out_of_sample_excess * 100, 2) : "n/a" },
      { label: "Adjusted p", num: true, cell: r => isNum(r.q_value) ? num(r.q_value, 3) : "n/a" },
      { label: "Verdict", cell: r => el("span", { title: r.verdict_text }, statusTag(VERDICTS[r.verdict][0], VERDICTS[r.verdict][1])) },
    ], s.results, { onRow: r => { current = r; drawChart(); chartBox.scrollIntoView({ block: "nearest" }); } }),
    el("div", { class: "row", style: "margin-top:14px" }, field("Chart symbol", symPick)), chartBox,
    el("h4", { text: "Read this before trusting a result" }), el("ul", { class: "plain small muted" }, s.caveats.map(c => el("li", { text: c }))),
    (s.data_warnings || []).map(w => banner("warn", w))));
  return frag;
}

/* ---------- view: backtest ---------- */
function buildBacktest() {
  const symbols = el("textarea", { "aria-label": "Symbols" });
  const waitForStatus = setInterval(() => { if (state.status) { symbols.value = state.status.watchlist.join(", "); clearInterval(waitForStatus); } }, 100);
  const n = (value, step, min, max) => el("input", { type: "number", value, step, min, max });
  const f = { years: n(5, 1, 2, 20), size: n(5, 1, 1, 100), maxPos: n(10, 1, 1, 50), stop: n(5, 1, 1, 50), target: n(15, 1, 1, 100),
    hold: n(60, 5, 1, 400), score: n(0.4, 0.05, 0, 1), confirm: n(3, 1, 1, 20), trials: n(1, 1, 1, 100000) };
  const shorts = segmented("bshorts", [["yes", "Long and short"], ["no", "Long only"]], "yes");
  const run = el("button", { class: "primary", text: "Run backtest" });
  const wf = el("button", { class: "plain", text: "Run walk-forward test" });
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const body = () => ({ symbols: symbols.value.split(/[\s,]+/).filter(Boolean), years: Number(f.years.value), n_trials: Number(f.trials.value),
    params: { position_size_pct: Number(f.size.value) / 100, max_positions: Number(f.maxPos.value), stop_loss_pct: Number(f.stop.value) / 100,
      take_profit_pct: Number(f.target.value) / 100, holding_period_days: Number(f.hold.value), min_score: Number(f.score.value),
      confirmation_days: Number(f.confirm.value), allow_shorts: shorts.value() === "yes" } });
  run.addEventListener("click", () => withBusy(run, error, async () => { out.replaceChildren(renderBacktest(await api("/api/backtest", body()))); }));
  wf.addEventListener("click", () => withBusy(wf, error, async () => { out.replaceChildren(renderWalkForward(await api("/api/walkforward", body()))); }));
  return el("section", {}, el("h2", { text: "Backtest the scanner strategy" }),
    el("p", { class: "lede", text: "Runs your 52-week mean-reversion score over real history, trading each signal at the next day's open with stops, targets and costs. The walk-forward test tunes the settings on one year, trades the next quarter with them, and repeats." }),
    el("div", { class: "panel" },
      el("div", { class: "row" }, field("Years of history", f.years), field("Position size (% of equity)", f.size), field("Most positions at once", f.maxPos),
        field("Stop loss (%)", f.stop), field("Take profit (%)", f.target), field("Longest hold (days)", f.hold),
        field("Lowest score to trade", f.score), field("Days a signal must persist", f.confirm), field("Directions", shorts),
        field("Variations you have tried", f.trials)),
      el("div", { style: "margin-top:12px" }, field("Symbols", symbols)),
      el("div", { class: "row", style: "margin-top:12px" }, run, wf),
      el("p", { class: "small muted", text: "Be honest about how many variations you have tried. Every re-run with new settings makes a good-looking result more likely to be luck, and the adjusted figure accounts for that." }), error), out);
}

function sharpeWord(sig) {
  if (!sig || !isNum(sig.deflated_sharpe)) return "n/a";
  return pct(sig.deflated_sharpe * 100, 0);
}

function renderBacktest(r) {
  const frag = el("div");
  const line = runLine(r.run);
  if (r.data_source === "demo") frag.append(banner("fail", "This run used made-up demo prices. The numbers show how the screen works, not how the strategy performs."));
  const b = r.benchmark;
  frag.append(el("div", { class: "panel" },
    el("h3", { text: `${shortDate(r.start_date)} to ${shortDate(r.end_date)}` }),
    el("div", { class: "metrics" },
      metric(signedPct(r.total_return_pct), `Total return (holding the stocks: ${signedPct(b.total_return_pct)})`),
      metric(isNum(r.sharpe) ? num(r.sharpe) : "n/a", `Sharpe ratio (holding: ${isNum(b.sharpe) ? num(b.sharpe) : "n/a"})`),
      metric(pct(r.max_drawdown_pct), `Worst drawdown (holding: ${pct(b.max_drawdown_pct)})`),
      metric(String(r.num_trades), "Trades"),
      metric(isNum(r.win_rate_pct) ? pct(r.win_rate_pct, 0) : "n/a", "Winning trades"),
      metric(isNum(r.profit_factor) ? num(r.profit_factor) : "n/a", "Profit factor"),
      metric(pct(r.avg_exposure_pct, 0), "Average capital in use"),
      metric(sharpeWord(r.significance), `Chance the Sharpe is real, after ${r.significance.n_trials} variation${r.significance.n_trials === 1 ? "" : "s"}`)),
    lineChart({ series: [
      { name: "Strategy", color: "--series-1", points: r.equity_curve.map(p => ({ x: p.date, y: p.equity })) },
      { name: "Buy and hold", color: "--series-2", points: r.equity_curve.map(p => ({ x: p.date, y: p.benchmark })) }],
      yFormat: v => money(v, 0) }),
    el("ul", { class: "plain small muted" }, r.notes.map(t => el("li", { text: t }))),
    (r.data_warnings || []).map(w => banner("warn", w))));
  const reasons = Object.entries(r.exit_reasons).sort((a, b2) => b2[1] - a[1]);
  const words = { STOP_LOSS: "Stop loss", STOP_LOSS_GAP: "Stop loss, gapped through", TAKE_PROFIT: "Take profit", TAKE_PROFIT_GAP: "Take profit, gapped through", HOLDING_PERIOD: "Held to the time limit", END_OF_TEST: "Open at the end of the test", DATA_ENDED: "Price data ended" };
  frag.append(el("div", { class: "panel" }, el("h3", { text: "How trades ended" }),
    table([{ label: "Exit", cell: x => words[x[0]] || x[0] }, { label: "Trades", num: true, cell: x => x[1] }], reasons),
    el("details", { style: "margin-top:12px" }, el("summary", { text: `Show the last ${Math.min(r.trades.length, 100)} trades` }),
      table([{ label: "Symbol", cell: t => t.symbol }, { label: "Side", cell: t => t.direction === "LONG" ? "Long" : "Short" },
        { label: "In", cell: t => t.entry_date }, { label: "Out", cell: t => t.exit_date },
        { label: "Entry", num: true, cell: t => money(t.entry_price) }, { label: "Exit", num: true, cell: t => money(t.exit_price) },
        { label: "Profit or loss", num: true, cell: t => `${money(t.pnl, 0)} (${signedPct(t.pnl_pct)})` },
        { label: "Why it closed", cell: t => words[t.exit_reason] || t.exit_reason }], r.trades.slice(-100).reverse()))));
  if (line) frag.append(line);
  return frag;
}

function renderWalkForward(w) {
  const frag = el("div"), o = w.out_of_sample;
  if (w.data_source === "demo") frag.append(banner("fail", "This run used made-up demo prices. The numbers show how the screen works, not how the strategy performs."));
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Walk-forward result (held-out quarters only)" }),
    el("div", { class: "metrics" },
      metric(signedPct(o.total_return_pct), "Total return across test windows"),
      metric(isNum(o.sharpe) ? num(o.sharpe) : "n/a", "Sharpe ratio in test windows"),
      metric(pct(o.max_drawdown_pct), "Worst drawdown"),
      metric(`${o.positive_folds} of ${o.folds}`, "Test windows that made money"),
      metric(isNum(w.avg_train_sharpe) ? num(w.avg_train_sharpe) : "n/a", "Average Sharpe while tuning"),
      metric(isNum(w.avg_test_sharpe) ? num(w.avg_test_sharpe) : "n/a", "Average Sharpe when traded"),
      metric(sharpeWord({ deflated_sharpe: w.significance.prob_sharpe_positive }), "Chance the held-out Sharpe is above zero")),
    table([
      { label: "Tuned on", cell: x => `${x.train_start} to ${x.train_end}` }, { label: "Traded", cell: x => `${x.test_start} to ${x.test_end}` },
      { label: "Settings chosen", cell: x => x.had_enough_trades ? Object.entries(x.chosen).map(([k, v]) => `${k.replace(/_pct$/, "").replace(/_/g, " ")} ${k.endsWith("_pct") ? pct(v * 100, 0) : v}`).join(", ") : "Too few trades to tune; defaults used" },
      { label: "Sharpe while tuning", num: true, cell: x => isNum(x.train_sharpe) ? num(x.train_sharpe) : "n/a" },
      { label: "Sharpe when traded", num: true, cell: x => isNum(x.test_sharpe) ? num(x.test_sharpe) : "n/a" },
      { label: "Return", num: true, cell: x => signedPct(x.test_return_pct) }, { label: "Trades", num: true, cell: x => x.test_trades }], w.folds),
    el("ul", { class: "plain small muted" }, w.notes.map(t => el("li", { text: t }))),
    (w.data_warnings || []).map(x => banner("warn", x))));
  const line = runLine(w.run);
  if (line) frag.append(line);
  return frag;
}

/* ---------- view: journal ---------- */
const RULE_WORDS = { earnings: "bought through earnings", earnings_estimate: "relied on an earnings date that disagrees with past filings", spread: "wide spread", dte: "too close to expiration",
  daily_loss: "traded past the daily loss stop", paid_above_plan: "paid more than the plan allowed",
  daily_loss_incomplete: "traded while today's loss could not be fully measured" };
function buildJournal() {
  const out = el("div");
  const error = el("div", { class: "error", role: "alert" });
  const snap = el("button", { class: "plain", text: "Record today's implied volatility" });
  const snapNote = el("span", { class: "small muted" });
  snap.addEventListener("click", () => withBusy(snap, error, async () => {
    const r = await api("/api/snapshot", {});
    snapNote.textContent = r.skipped ? r.skipped : `Saved ${r.saved.length} symbols for ${r.date}` + (r.failed.length ? `; ${r.failed.length} failed (${r.failed[0].symbol}: ${r.failed[0].error})` : ".");
  }));
  state.refreshJournal = async () => {
    try { out.replaceChildren(renderJournal(await api("/api/journal"), error)); error.textContent = ""; }
    catch (e) { error.textContent = e.message; }
  };
  return el("section", {}, el("h2", { text: "Journal" }),
    el("p", { class: "lede", text: "Every planned trade, each limit price you tried, and how it ended. This is where you find out whether your fills, your signals and your rule-breaking are helping or hurting." }),
    error, out,
    el("div", { class: "panel" }, el("h3", { text: "Daily volatility record" }),
      el("p", { class: "small muted", text: "Implied volatility rank needs a daily record of each stock's implied volatility. Record it once a day here, or schedule 'python run.py --snapshot'." }),
      el("div", { class: "row" }, snap, snapNote)));
}

function renderJournal(d, error) {
  const frag = el("div"), s = d.stats, o = s.overall;
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Results so far" }),
    el("div", { class: "metrics" },
      metric(money(s.open_risk, 0), "Premium at risk in open trades"), metric(money(s.planned_risk, 0), "Committed by saved plans"),
      metric(money(s.realized_today, 0), "Closed today"),
      metric(String(o.trades), "Closed trades"), metric(isNum(o.win_rate) ? pct(o.win_rate * 100, 0) : "n/a", "Winners"),
      metric(money(o.total_pnl, 0), "Total profit or loss"),
      metric(s.followed_rules.trades ? money(s.followed_rules.avg_pnl, 0) : "n/a", `Average when inside your limits (${s.followed_rules.trades})`),
      metric(s.overrode_a_limit.trades ? money(s.overrode_a_limit.avg_pnl, 0) : "n/a", `Average when you overrode one (${s.overrode_a_limit.trades})`)),
    s.by_signal.length ? [el("h4", { text: "By pattern present at entry" }), table([{ label: "Pattern", cell: r => r.signal },
      { label: "Trades", num: true, cell: r => r.trades }, { label: "Winners", num: true, cell: r => pct(r.win_rate * 100, 0) },
      { label: "Total", num: true, cell: r => money(r.total_pnl, 0) }], s.by_signal)] : null));
  const cal = d.calibration;
  frag.append(el("div", { class: "panel" }, el("h3", { text: "How your buy limits have filled" }),
    table([{ label: "Limit placed", cell: b => b.label[0].toUpperCase() + b.label.slice(1) }, { label: "Tries", num: true, cell: b => b.attempts },
      { label: "Filled", num: true, cell: b => isNum(b.fill_rate) ? pct(b.fill_rate * 100, 0) : "n/a" },
      { label: "Typical wait", num: true, cell: b => isNum(b.median_seconds) ? `${num(b.median_seconds, 0)} s` : "n/a" }], cal.buckets),
    el("p", { class: "small muted", text: cal.note })));

  const list = el("div", { class: "panel" }, el("h3", { text: "Trades" }));
  if (!d.trades.length) list.append(el("p", { class: "muted", text: "Nothing here yet. Price an option, write the ticket, and save it to start the journal." }));
  const words = { planned: "Planned", open: "Open", closed: "Closed", cancelled: "Cancelled" };
  for (const t of d.trades) {
    const title = `${t.symbol} ${shortDate(t.expiration)} ${num(t.strike, t.strike % 1 ? 2 : 0)} ${t.option_type} × ${t.contracts}`;
    const result = t.status === "closed" ? `${money(t.pnl, 0)}` : t.status === "open" ? `in at ${money(t.entry_price)}` : `plan ${money(t.planned_limit)} to ${money(t.max_limit)}`;
    const body = el("div", { style: "padding:8px 0 4px" });
    body.append(el("p", { class: "small" }, el("strong", { text: "Thesis: " }), t.thesis || "none"),
      el("p", { class: "small muted", text: `Saved ${shortDate(t.created_at)} from ${t.data_source} data. Quote then: bid ${money(t.quote.bid)}, ask ${money(t.quote.ask)}, stock ${money(t.quote.spot)}. ` +
        `Plan: take profit at ${money(t.plan.profit_target_price)}, stop at ${money(t.plan.stop_price)}, out by ${shortDate(t.plan.exit_by)}.` +
        (t.overrides.length ? ` Rules broken: ${t.overrides.map(o => RULE_WORDS[o] || o).join(", ")}.` : "") + (t.notes ? ` Notes: ${t.notes}` : "") }));
    const act = async (path, payload) => { try { await api(`/api/journal/${t.id}/${path}`, payload); await state.refreshJournal(); } catch (e) { error.textContent = e.message; error.scrollIntoView({ block: "nearest" }); } };
    if (t.status === "planned" || t.status === "open") {
      const sideWord = t.status === "planned" ? "buy" : "sell";
      const bid = el("input", { type: "number", step: "0.01", min: "0" }), ask = el("input", { type: "number", step: "0.01", min: "0" });
      const lim = el("input", { type: "number", step: "0.01", min: "0.01" }), secs = el("input", { type: "number", step: "1", min: "0" });
      const filled = segmented(`fill${t.id}`, [["yes", "Filled"], ["no", "Did not fill"]], "no");
      const log = el("button", { class: "plain", text: `Log this ${sideWord} attempt` });
      log.addEventListener("click", () => act("attempt", { side: sideWord, bid: Number(bid.value), ask: Number(ask.value), limit_price: Number(lim.value),
        filled: filled.value() === "yes", seconds_to_fill: secs.value === "" ? null : Number(secs.value) }));
      body.append(el("div", { class: "inline-form" }, field("Bid you saw", bid), field("Ask you saw", ask), field("Your limit", lim), field("Result", filled), field("Seconds until filled", secs), log));
      if (t.status === "planned") {
        const cancel = el("button", { class: "plain", text: "Cancel this plan" });
        cancel.addEventListener("click", () => act("cancel", { note: "" }));
        body.append(cancel);
      } else {
        const px = el("input", { type: "number", step: "0.01", min: "0", value: 0 });
        const close = el("button", { class: "plain", text: "Close without a sell order" });
        close.addEventListener("click", () => act("close", { exit_price: Number(px.value), note: "closed without a logged order" }));
        body.append(el("div", { class: "inline-form" }, field("Value at close (0 if it expired worthless)", px), close));
      }
    }
    list.append(el("details", {}, el("summary", {}, el("strong", { text: `${words[t.status]}: ` }), `${title}, ${result}` + (t.attempt_count ? `, ${t.attempt_count} attempt${t.attempt_count === 1 ? "" : "s"} logged` : "")), body));
  }
  frag.append(list);
  return frag;
}

/* ---------- start ---------- */
const BUILDERS = { price: buildPrice, simulate: buildSimulate, research: buildResearch, patterns: buildPatterns, backtest: buildBacktest, journal: buildJournal };
// Open the simulator on a position, building the screen first if it has not been shown yet.
state.openSimulation = (symbol, legs) => {
  if (state.views.simulate) { show("simulate"); state.views.simulate.loadPosition(symbol, legs); }
  else { state.pendingSim = { symbol, legs }; show("simulate"); }
};
// Open Price an option on a contract and price it straight away (2.5). `spec` comes from FLOW.pricingRequest.
state.openPricing = spec => {
  if (state.views.price) { show("price"); state.views.price.loadContract(spec); }
  else { state.pendingPrice = spec; show("price"); }
};
state.refreshJournal = async () => {};
// Started once every script on the page has run, so later scripts (v2.js) can add screens to BUILDERS.
// Screens behind a V2 feature flag hide their button when the flag is off.
async function start() {
  for (const btn of document.querySelectorAll(".rail button.nav")) btn.addEventListener("click", () => show(btn.dataset.view));
  await loadStatus();
  const features = (state.status && state.status.features) || {};
  for (const btn of document.querySelectorAll(".rail button.nav[data-feature]")) {
    const on = features[btn.dataset.feature] === true && BUILDERS[btn.dataset.view];
    btn.classList.toggle("hidden", !on);
    if (!on) delete BUILDERS[btn.dataset.view];
  }
  show(BUILDERS[location.hash.slice(1)] ? location.hash.slice(1) : "price");
}
document.addEventListener("DOMContentLoaded", start);
