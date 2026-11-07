"use strict";
/* Trade Assistant 3.0 workstation: four workspaces of movable, resizable widgets that share one context per link
   group (the selected stock, direction, horizon, contract, strategy, scenario and draft proposal). Selecting a
   stock in any widget updates every widget linked to it, in every workspace. Layouts and the shared context are
   saved on the server, so the workstation reopens as it was left, drafts included.

   Everything is under the one global WS; the classic screens' scripts are loaded first and their renderers run
   inside widgets. */
const WS = (() => {
  const GROUPS = [["blue", "Blue"], ["green", "Green"], ["amber", "Amber"], ["plum", "Plum"], ["none", "Not linked"]];
  const WORKSPACES = { discover: "Discover", research: "Research", desk: "Trading Desk", portfolio: "Portfolio" };
  const CONTEXT_KEYS = ["symbol", "direction", "horizon", "contract", "strategy", "scenario", "proposal", "opportunity", "study"];
  const TYPES = {};                       // widget type -> definition
  const ctx = {};                         // link group -> shared context
  const providers = {};                   // link group -> {capability: {widget, api}}
  const tabs = new Map();                 // tab id -> {id, workspace, name, layoutId, root, grid, widgets: Map}
  const eventHandlers = [];               // [topicPrefix, fn]
  let activeTab = null, focusedWidget = null, dockWidget = null, saveTimer = null, booted = false, restoring = false;
  let symbolsList = [], statusInfo = null, lastEvent = 0;
  let dockOpen = true;

  for (const [g] of GROUPS) ctx[g] = {};

  /* ---------- small DOM helpers (classic el() does the work) ---------- */
  const icon = path => { const s = svg("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }); s.append(svg("path", { d: path })); return s; };
  const ICONS = { refresh: "M13 8a5 5 0 1 1-1.5-3.5M13 2.5V5h-2.5", max: "M3 6V3h3M10 3h3v3M13 10v3h-3M6 13H3v-3",
    restore: "M6 3v3H3M10 3v3h3M10 13v-3h3M6 13v-3H3", collapse: "M3 8h10", expand: "M3 6l5 5 5-5", close: "M4 4l8 8M12 4l-8 8",
    dock: "M3 3h10v10H3zM9 3v10" };
  const btn = (name, label, onclick) => { const b = el("button", { class: "ws-wbtn", type: "button", title: label, "aria-label": label, onclick }); b.append(icon(ICONS[name])); return b; };
  const empty = text => el("p", { class: "ws-empty", text });
  let toastTimer = null;
  function toast(text) {
    const t = document.getElementById("ws-toast");
    t.textContent = text; t.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
  }

  /* ---------- the shared context ---------- */
  function contextOf(w) { return w.group === "none" ? (w.own || (w.own = {})) : ctx[w.group]; }

  // Change a link group's context and tell every widget linked to it (but not the one that made the change).
  function set(group, patch, origin) {
    const target = group === "none" ? (origin && (origin.own || (origin.own = {}))) : ctx[group];
    if (!target) return;
    if (patch.symbol !== undefined && patch.symbol) patch.symbol = String(patch.symbol).trim().toUpperCase();
    // A new stock clears what belonged to the old one, unless the change names it too.
    if (patch.symbol && patch.symbol !== target.symbol) {
      for (const k of ["contract", "strategy", "scenario", "proposal", "opportunity", "study"]) if (!(k in patch)) patch[k] = null;
    }
    const changed = Object.keys(patch).filter(k => JSON.stringify(target[k]) !== JSON.stringify(patch[k]));
    if (!changed.length) return;
    Object.assign(target, patch);
    for (const w of allWidgets()) {
      if (w === origin && !patch._self) continue;
      if (group === "none" ? w !== origin : w.group !== group) continue;
      deliver(w, changed);
    }
    saveSoon();
  }

  function deliver(w, changed) {
    if (!w.impl || !w.impl.onContext) return;
    try { w.impl.onContext(contextOf(w), changed); }
    catch (e) { console.error(e); w.fail(e); }
    w.renderBadge();
  }

  // A widget offers something its linked widgets can use (the chart offers its API for the scenario lines).
  function provide(w, capability, apiObj) {
    const g = w.group;
    providers[g] = providers[g] || {};
    if (apiObj) providers[g][capability] = { widget: w, api: apiObj };
    else if (providers[g][capability] && providers[g][capability].widget === w) delete providers[g][capability];
    for (const other of allWidgets()) if (other !== w && other.group === g && other.impl && other.impl.onProvider) other.impl.onProvider(capability);
  }
  function find(w, capability) {
    const hit = (providers[w.group] || {})[capability];
    return hit && hit.widget !== w ? hit.api : null;
  }

  /* ---------- the widget registry ---------- */
  function register(type, def) { TYPES[type] = { type, size: { w: 6, h: 6 }, category: "Research", ...def }; }
  function catalog() {
    const features = (state.status && state.status.features) || {};
    return Object.values(TYPES).filter(d => !d.hidden && (d.needs || []).every(f => features[f] === true));
  }
  function allWidgets() {
    const out = [];
    for (const t of tabs.values()) out.push(...t.widgets.values());
    if (dockWidget) out.push(dockWidget);
    return out;
  }

  /* ---------- a widget's frame ---------- */
  function frame(spec, host) {
    const def = TYPES[spec.type] || TYPES.unknown;
    const w = { id: spec.id || `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, type: spec.type,
      group: spec.group || def.group || "blue", config: { ...(def.config || {}), ...(spec.config || {}) }, collapsed: !!spec.collapsed,
      def, host, impl: null, own: spec.own || null };
    const linkBtn = el("button", { class: "ws-link", type: "button" });
    const title = el("span", { class: "ws-wtitle", text: def.title });
    const badge = el("span", { class: "ws-badge" });
    const head = el("div", { class: "ws-whead" }, linkBtn, title, badge, el("span", { class: "ws-gap" }));
    const body = el("div", { class: "ws-wbody" });
    const node = el("div", { class: "ws-widget", "data-group": w.group, "data-type": w.type, "data-wid": w.id }, head, body);
    const tools = el("span", { class: "ws-wtools" },
      btn("refresh", "Refresh", () => w.refresh()),
      def.dockable === false ? null : btn("dock", host === "dock" ? "Move into the workspace" : "Pin to the dock", () => (host === "dock" ? undock() : toDock(w))),
      host === "dock" ? null : btn("collapse", "Collapse", () => w.toggleCollapse()),
      host === "dock" ? null : btn("max", "Maximize", () => w.toggleMax()),
      btn("close", "Close", () => w.close()));
    head.append(tools);
    Object.assign(w, { node, head, body, titleEl: title, badgeEl: badge, linkBtn });
    const groupLabel = () => (GROUPS.find(g => g[0] === w.group) || GROUPS[0])[1];
    const linkLabel = () => { linkBtn.title = `Link: ${groupLabel()}. Click to change`; linkBtn.setAttribute("aria-label", `Link group ${groupLabel()}; change`); };
    linkLabel();
    linkBtn.addEventListener("click", e => { e.stopPropagation(); openMenu(linkBtn, GROUPS.map(([g, label]) => [label, () => w.setGroup(g)])); });
    node.addEventListener("pointerdown", () => focus(w), true);
    w.setTitle = text => { title.textContent = text || def.title; };
    w.setBadge = text => { w.badgeText = text; w.renderBadge(); };
    w.renderBadge = () => {
      const c = contextOf(w);
      badge.textContent = w.badgeText !== undefined ? (w.badgeText || "") : (def.symbolBadge === false ? "" : (c.symbol || ""));
    };
    w.ctx = () => contextOf(w);
    w.set = patch => set(w.group, patch, w);
    w.save = () => saveSoon();
    w.fail = e => body.replaceChildren(el("div", { class: "error", role: "alert", text: (e && e.message) || String(e) }));
    w.setGroup = g => {
      if (g === w.group) return;
      // What it offered its old group (a chart's API) it now offers the new one.
      const offered = Object.entries(providers[w.group] || {}).filter(([, p]) => p.widget === w).map(([cap, p]) => [cap, p.api]);
      for (const [cap] of offered) provide(w, cap, null);
      w.group = g; node.dataset.group = g; linkLabel();
      for (const [cap, a] of offered) provide(w, cap, a);
      deliver(w, CONTEXT_KEYS);
      if (w.impl && w.impl.onProvider) w.impl.onProvider("*");
      saveSoon();
    };
    w.refresh = () => { if (w.impl && w.impl.refresh) { try { w.impl.refresh(); } catch (e) { w.fail(e); } } else deliver(w, CONTEXT_KEYS); };
    w.toggleCollapse = () => {
      w.collapsed = !w.collapsed; node.classList.toggle("collapsed", w.collapsed);
      const item = node.closest(".grid-stack-item"), tab = tabs.get(w.host);
      if (item && tab) {
        if (w.collapsed) { w.fullH = Number(item.getAttribute("gs-h")) || def.size.h; tab.grid.update(item, { h: 1 }); }
        else tab.grid.update(item, { h: w.fullH || def.size.h });
      }
      saveSoon();
    };
    w.toggleMax = () => {
      const item = node.closest(".grid-stack-item");
      if (!item) return;
      const on = !item.classList.contains("ws-max");
      for (const other of document.querySelectorAll(".grid-stack-item.ws-max")) other.classList.remove("ws-max");
      item.classList.toggle("ws-max", on);
      const b = head.querySelector('[aria-label="Maximize"], [aria-label="Restore"]');
      if (b) { b.setAttribute("aria-label", on ? "Restore" : "Maximize"); b.title = on ? "Restore" : "Maximize"; b.replaceChildren(icon(ICONS[on ? "restore" : "max"])); }
      if (w.impl && w.impl.onResize) w.impl.onResize();
    };
    w.close = () => removeWidget(w);
    if (w.collapsed) node.classList.add("collapsed");
    return w;
  }

  function start(w) {
    w.pending = false;
    const features = (state.status && state.status.features) || {};
    const off = (w.def.needs || []).filter(f => features[f] !== true);
    if (off.length) {
      w.impl = {};
      w.body.replaceChildren(empty(`This widget needs ${off.map(f => f.replace(/_/g, " ")).join(" and ")}, switched off in .env (FEATURE_${off[0].toUpperCase()}=0).`));
      return;
    }
    try {
      w.impl = w.def.create(w) || {};
      deliver(w, CONTEXT_KEYS);
    } catch (e) { console.error(e); w.fail(e); }
    w.renderBadge();
  }

  function focus(w) {
    if (focusedWidget === w) return;
    if (focusedWidget && focusedWidget.node) focusedWidget.node.classList.remove("focused");
    focusedWidget = w; w.node.classList.add("focused");
  }

  function removeWidget(w, quiet) {
    try { if (w.impl && w.impl.dispose) w.impl.dispose(); } catch (e) { console.error(e); }
    for (const g of Object.keys(providers)) for (const cap of Object.keys(providers[g])) if (providers[g][cap].widget === w) provide(w, cap, null);
    if (w.host === "dock") { dockWidget = null; renderDock(); }
    else {
      const tab = tabs.get(w.host);
      if (tab) { const item = w.node.closest(".grid-stack-item"); tab.widgets.delete(w.id); if (item) tab.grid.removeWidget(item, true); }
    }
    if (focusedWidget === w) focusedWidget = null;
    if (!quiet) saveSoon();
  }

  /* ---------- workspaces as tabs ---------- */
  function gridOptions() {
    return { column: 12, cellHeight: 56, margin: 0, float: false, handle: ".ws-whead", animate: true,
      draggable: { cancel: "button, input, select, textarea, a, [contenteditable]" },
      resizable: { handles: "e,se,s,sw,w" } };
  }

  // A phone shows one column, in reading order (row by row, left to right). The grid is always built at twelve
  // columns first, so the saved places are kept; saving in one column still saves the twelve-column layout.
  const NARROW = 820;
  function applyColumns(tab) {
    const want = window.innerWidth <= NARROW ? 1 : 12;
    if (tab.grid.getColumn() !== want) tab.grid.column(want, want === 1 ? "list" : "moveScale");
  }

  function openTab(spec, makeActive = true) {
    const id = spec.id || `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
    const gridEl = el("div", { class: "grid-stack" });
    const root = el("section", { class: "ws-tabset hidden", "data-tab": id, "data-workspace": spec.workspace, "aria-label": spec.name }, gridEl);
    document.getElementById("ws-tabsets").append(root);
    const grid = GridStack.init(gridOptions(), gridEl);
    const tab = { id, workspace: spec.workspace, name: spec.name || WORKSPACES[spec.workspace], layoutId: spec.layoutId || null, root, grid, widgets: new Map() };
    tabs.set(id, tab);
    grid.on("change", () => saveSoon());
    grid.on("resizestop", (_e, item) => { const w = tab.widgets.get(item.getAttribute("gs-id")); if (w && w.impl && w.impl.onResize) w.impl.onResize(); });
    grid.batchUpdate();
    for (const s of [...(spec.widgets || [])].sort((a, b) => ((a.y ?? 0) - (b.y ?? 0)) || ((a.x ?? 0) - (b.x ?? 0)))) addWidget(tab, s);
    grid.batchUpdate(false);
    applyColumns(tab);
    renderTabs();
    if (makeActive) activate(id);
    return tab;
  }

  function addWidget(tab, spec) {
    if (!TYPES[spec.type]) spec = { ...spec, config: { ...(spec.config || {}), missing: spec.type }, type: "unknown" };
    const def = TYPES[spec.type];
    const w = frame(spec, tab.id);
    const content = el("div", { class: "grid-stack-item-content" }, w.node);
    const item = el("div", { class: "grid-stack-item", "gs-id": w.id, "gs-w": String(spec.w || def.size.w), "gs-h": String(w.collapsed ? 1 : (spec.h || def.size.h)),
      "gs-min-w": String(def.minW || 2), "gs-min-h": "1" }, content);
    if (spec.x === undefined || spec.y === undefined) item.setAttribute("gs-auto-position", "true");
    else { item.setAttribute("gs-x", String(spec.x)); item.setAttribute("gs-y", String(spec.y)); }
    if (w.collapsed) w.fullH = spec.fullH || spec.h || def.size.h;
    tab.root.querySelector(".grid-stack").append(item);
    tab.grid.makeWidget(item);
    tab.widgets.set(w.id, w);
    // A workspace that is not in front starts its widgets when it is first shown, so opening four at once is quick.
    if (tab.id === activeTab) start(w); else w.pending = true;
    return w;
  }

  function activate(id) {
    if (!tabs.has(id)) return;
    activeTab = id;
    for (const t of tabs.values()) t.root.classList.toggle("hidden", t.id !== id);
    for (const b of document.querySelectorAll(".ws-tab-btn")) b.setAttribute("aria-selected", b.dataset.tab === id ? "true" : "false");
    const t = tabs.get(id);
    for (const w of t.widgets.values()) {
      if (w.pending) start(w);
      else if (w.impl && w.impl.onShow) w.impl.onShow();
    }
    document.title = `${t.name} · Trade Assistant`;
    saveSoon();
  }

  function closeTab(id) {
    const t = tabs.get(id);
    if (!t) return;
    if (tabs.size === 1) { toast("Keep at least one workspace open."); return; }
    for (const w of [...t.widgets.values()]) removeWidget(w, true);
    t.grid.destroy(true); t.root.remove(); tabs.delete(id);
    if (activeTab === id) activate(tabs.keys().next().value);
    renderTabs(); saveSoon();
  }

  function renderTabs() {
    const bar = document.getElementById("ws-tabs");
    bar.replaceChildren(...[...tabs.values()].map(t => {
      const x = el("span", { class: "ws-x", role: "button", "aria-label": `Close ${t.name}`, title: "Close this workspace", text: "×",
        onclick: e => { e.stopPropagation(); closeTab(t.id); } });
      return el("button", { class: "ws-tab-btn", type: "button", role: "tab", "data-tab": t.id, "data-workspace": t.workspace,
        "aria-selected": t.id === activeTab ? "true" : "false", onclick: () => activate(t.id) }, t.name, x);
    }));
  }

  function tabSpec(t) {
    const saved = t.grid.save(false);
    const widgets = saved.map(n => {
      const w = t.widgets.get(n.id);
      if (!w) return null;
      const config = w.impl && w.impl.config ? { ...w.config, ...w.impl.config() } : w.config;
      return { id: w.id, type: w.type, x: n.x || 0, y: n.y || 0, w: n.w || 1, h: w.collapsed ? (w.fullH || n.h || 1) : (n.h || 1),
        group: w.group, collapsed: w.collapsed, config, ...(w.group === "none" && w.own ? { own: w.own } : {}) };
    }).filter(Boolean);
    return { id: t.id, workspace: t.workspace, name: t.name, layoutId: t.layoutId, widgets };
  }

  /* ---------- the dock: one widget kept in view across workspaces ---------- */
  function toDock(w) {
    const spec = { type: w.type, group: w.group, config: w.impl && w.impl.config ? { ...w.config, ...w.impl.config() } : w.config, own: w.own };
    removeWidget(w, true);
    if (dockWidget) { const old = dockWidget; removeWidget(old, true); addWidget(tabs.get(activeTab), { type: old.type, group: old.group, config: old.config }); }
    mountDock(spec);
    setDockOpen(true);
    saveSoon();
  }
  function undock() {
    if (!dockWidget) return;
    const d = dockWidget, spec = { type: d.type, group: d.group, config: d.impl && d.impl.config ? { ...d.config, ...d.impl.config() } : d.config };
    removeWidget(d, true);
    addWidget(tabs.get(activeTab), spec);
    saveSoon();
  }
  function mountDock(spec) {
    if (!spec || !TYPES[spec.type]) { dockWidget = null; renderDock(); return; }
    const w = frame({ ...spec, id: spec.id || "dock" }, "dock");
    dockWidget = w;
    renderDock();
    start(w);
  }
  function renderDock() {
    const dock = document.getElementById("ws-dock");
    dock.replaceChildren(dockWidget ? dockWidget.node : el("div", { class: "ws-dock-empty" },
      el("p", { text: "The dock keeps one widget in view whichever workspace is open: pin the order ticket here while you research." })));
  }
  function setDockOpen(on) {
    dockOpen = on;
    document.getElementById("ws-dock").classList.toggle("closed", !on);
    const b = document.getElementById("ws-dock-toggle");
    if (b) { b.textContent = on ? "Hide dock" : "Show dock"; b.setAttribute("aria-pressed", on ? "true" : "false"); }
    saveSoon();
  }

  /* ---------- saving ---------- */
  function sessionBody() {
    const dock = dockWidget ? { type: dockWidget.type, group: dockWidget.group,
      config: dockWidget.impl && dockWidget.impl.config ? { ...dockWidget.config, ...dockWidget.impl.config() } : dockWidget.config } : null;
    return { tabs: [...tabs.values()].map(tabSpec), active: activeTab, dock: { widget: dock, open: dockOpen }, context: ctx };
  }
  function saveSoon() {
    if (!booted || restoring) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 700);
  }
  async function saveNow() {
    clearTimeout(saveTimer); saveTimer = null;
    try { await api("/v3/workspaces/session", sessionBody()); }
    catch (e) { console.warn("The workstation could not be saved:", e.message); }
  }

  async function saveLayout(asNew, name) {
    const t = tabs.get(activeTab);
    const spec = tabSpec(t);
    const body = { workspace: t.workspace, name: name || t.name, widgets: spec.widgets.map(({ own, ...w }) => w), context: ctx,
      ...(t.layoutId && !asNew ? { id: t.layoutId } : {}) };
    const saved = await api("/v3/workspaces", body);
    t.layoutId = saved.id; t.name = saved.name;
    renderTabs(); saveSoon();
    toast(`Saved the layout "${saved.name}".`);
    return saved;
  }

  async function openLayout(id) {
    const l = await api(`/v3/workspaces/${encodeURIComponent(id)}`);
    openTab({ workspace: l.workspace, name: l.name, layoutId: l.id, widgets: l.widgets.map(w => ({ ...w, id: undefined })) });
  }

  function resetTab() {
    const t = tabs.get(activeTab);
    for (const w of [...t.widgets.values()]) removeWidget(w, true);
    t.grid.batchUpdate();
    for (const s of WS.DEFAULT_LAYOUTS[t.workspace] || []) addWidget(t, { ...s });
    t.grid.batchUpdate(false);
    applyColumns(t);
    saveSoon();
  }

  /* ---------- menus and the command palette ---------- */
  let menuEl = null;
  function openMenu(anchor, items) {
    closeMenu();
    menuEl = el("div", { class: "ws-menu", role: "menu" });
    for (const it of items) {
      if (it === "-") menuEl.append(el("hr"));
      else if (typeof it === "string") menuEl.append(el("div", { class: "ws-menu-h", text: it }));
      else if (it.nodeType) menuEl.append(it);
      else menuEl.append(el("button", { type: "button", role: "menuitem", onclick: () => { closeMenu(); it[1](); } }, it[0]));
    }
    document.body.append(menuEl);
    const r = anchor.getBoundingClientRect();
    const left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - 260);
    menuEl.style.left = `${Math.max(8, left)}px`; menuEl.style.top = `${r.bottom + window.scrollY + 4}px`;
    const first = menuEl.querySelector("button, input"); if (first) first.focus();
    setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
  }
  function outside(e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }
  function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } document.removeEventListener("pointerdown", outside, true); }

  function widgetMenu(anchor) {
    const groups = {};
    for (const d of catalog()) (groups[d.category] = groups[d.category] || []).push(d);
    const items = [];
    for (const [cat, defs] of Object.entries(groups)) {
      items.push(cat);
      for (const d of defs) items.push([d.title, () => addToActive(d.type)]);
    }
    openMenu(anchor, items);
  }
  function addToActive(type, config) {
    const t = tabs.get(activeTab);
    const w = addWidget(t, { type, group: focusedWidget && focusedWidget.group !== "none" ? focusedWidget.group : "blue", config });
    focus(w);
    w.node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    saveSoon();
    return w;
  }

  function layoutMenu(anchor) {
    const name = el("input", { type: "text", "aria-label": "Layout name", placeholder: "Name this layout", value: tabs.get(activeTab).name });
    const form = el("div", { class: "ws-menu-form" }, name, el("button", { class: "ws-btn", type: "button", onclick: async () => {
      closeMenu(); try { await saveLayout(true, name.value.trim() || tabs.get(activeTab).name); } catch (e) { toast(e.message); } } }, "Save as"));
    const items = [["Save this layout", async () => { try { await saveLayout(false); } catch (e) { toast(e.message); } }], form, "-",
      ["Reset to the standard layout", resetTab], "-", "Open a workspace"];
    for (const [k, label] of Object.entries(WORKSPACES)) items.push([`New ${label}`, () => openTab({ workspace: k, name: label, widgets: WS.DEFAULT_LAYOUTS[k] || [] })]);
    openMenu(anchor, items);
    api("/v3/workspaces").then(d => {
      if (!menuEl || !d.layouts.length) return;
      menuEl.append(el("hr"), el("div", { class: "ws-menu-h", text: "Saved layouts" }),
        ...d.layouts.map(l => el("button", { type: "button", role: "menuitem", onclick: () => { closeMenu(); openLayout(l.id).catch(e => toast(e.message)); } },
          `${l.name} (${WORKSPACES[l.workspace]})`)));
    }).catch(() => {});
  }

  function classicMenu(anchor) {
    const views = [["morning", "Morning plan"], ["live", "Live news"], ["today", "Today"], ["market", "Market & news"], ["ideas", "Trade ideas"],
      ["scanner", "Scanner"], ["analyze", "Analyze opportunity"], ["price", "Price an option"], ["simulate", "Simulate a trade"],
      ["lab", "Options lab"], ["positions", "Positions"], ["portfolio", "Portfolio risk"], ["paper", "Paper trading"],
      ["trades", "My trades"], ["journal", "Journal"], ["patterns", "Pattern lab"], ["backtest", "Backtest"], ["research", "Research a stock"],
      ["health", "Data health"]].filter(([v]) => BUILDERS[v]);
    openMenu(anchor, ["Open in this workspace", ...views.map(([v, label]) => [label, () => routeClassic(v)]), "-",
      ["Open the classic screens in a new tab", () => window.open("/classic", "_blank", "noopener")]]);
  }

  function routeClassic(view) {
    for (const t of tabs.values()) for (const w of t.widgets.values()) if (w.type === `classic:${view}`) { activate(t.id); focus(w); w.node.scrollIntoView({ block: "nearest" }); return; }
    if (dockWidget && dockWidget.type === `classic:${view}`) { setDockOpen(true); return; }
    addToActive(`classic:${view}`);
  }

  let palette = null;
  function openPalette(prefill) {
    const overlay = document.getElementById("ws-palette");
    const input = overlay.querySelector("input"), list = overlay.querySelector("ul");
    overlay.classList.remove("hidden");
    input.value = prefill || ""; input.focus();
    let sel = 0, items = [];
    const commands = () => [
      ...Object.entries(WORKSPACES).map(([k, label]) => ({ label: `Go to ${label}`, kind: "workspace", run: () => goWorkspace(k) })),
      ...catalog().map(d => ({ label: `Add widget: ${d.title}`, kind: "widget", run: () => addToActive(d.type) })),
      { label: "Save this layout", kind: "layout", run: () => saveLayout(false).catch(e => toast(e.message)) },
      { label: dockOpen ? "Hide the dock" : "Show the dock", kind: "dock", run: () => setDockOpen(!dockOpen) },
      { label: "Open the classic screens", kind: "classic", run: () => window.open("/classic", "_blank", "noopener") }];
    const draw = () => {
      const q = input.value.trim().toUpperCase();
      const syms = q ? symbolsList.filter(s => s.startsWith(q)).slice(0, 8) : [];
      const exact = q && /^[A-Z]{1,5}(\.[A-Z])?$/.test(q) && !syms.includes(q) ? [q] : [];
      const group = focusedWidget && focusedWidget.group !== "none" ? focusedWidget.group : "blue";
      items = [...[...exact, ...syms].map(s => ({ label: s, kind: `open in the ${(GROUPS.find(g => g[0] === group) || GROUPS[0])[1].toLowerCase()} link`,
        run: () => set(group, { symbol: s }) })),
        ...commands().filter(c => !q || c.label.toUpperCase().includes(q))].slice(0, 14);
      sel = Math.min(sel, Math.max(0, items.length - 1));
      list.replaceChildren(...items.map((it, i) => el("li", { role: "option", "aria-selected": i === sel ? "true" : "false",
        onclick: () => { closePalette(); it.run(); } }, el("span", { text: it.label }), el("span", { class: "k", text: it.kind }))));
    };
    palette = { draw, key: e => {
      if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); draw(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); e.preventDefault(); }
      else if (e.key === "Enter") { const it = items[sel]; closePalette(); if (it) it.run(); e.preventDefault(); }
      else if (e.key === "Escape") { closePalette(); e.preventDefault(); }
    } };
    input.oninput = () => { sel = 0; draw(); };
    input.onkeydown = palette.key;
    draw();
  }
  function closePalette() { document.getElementById("ws-palette").classList.add("hidden"); palette = null; }

  function goWorkspace(kind) {
    for (const t of tabs.values()) if (t.workspace === kind) { activate(t.id); return t; }
    return openTab({ workspace: kind, name: WORKSPACES[kind], widgets: WS.DEFAULT_LAYOUTS[kind] || [] });
  }

  /* ---------- the status strip and the event stream ---------- */
  async function refreshStatus() {
    try {
      const [s, v3] = await Promise.all([api("/api/status"), api("/v3/status")]);
      state.status = s; statusInfo = v3;
      const clock = { open: ["pass", "Market open"], closed: ["", "Market closed"], pre: ["warn", "Pre-market"], post: ["warn", "After hours"],
        premarket: ["warn", "Pre-market"], postmarket: ["warn", "After hours"] }[s.clock.state] || ["", "Market hours unknown"];
      const data = s.data.mode === "demo" ? ["fail", "Made-up prices"] : s.data.realtime ? ["pass", "Real-time quotes"] : ["warn", "Delayed 15 minutes"];
      const trading = v3.trading || { mode: "advisory", label: "Advisory: no orders are sent", detail: "" };
      const parts = [
        el("span", { class: "ws-pill" }, el("span", { class: `ws-dot ${data[0]}` }), el("b", { text: s.data.label }), data[1]),
        el("span", { class: "ws-pill" }, el("span", { class: `ws-dot ${clock[0]}` }), clock[1]),
        el("span", { class: "ws-pill", title: trading.detail || "" }, el("span", { class: `ws-mode ${trading.mode}`, id: "ws-mode", text: trading.label })),
      ];
      if (v3.risk) parts.push(el("span", { class: "ws-pill", title: v3.risk.detail || "" }, el("span", { class: `ws-dot ${v3.risk.level || ""}` }), v3.risk.label));
      if (v3.daemon) parts.push(el("span", { class: "ws-pill", title: v3.daemon.detail || "" }, el("span", { class: `ws-dot ${v3.daemon.level || ""}` }), v3.daemon.label));
      parts.push(el("span", { class: "ws-spacer" }),
        el("span", { class: "ws-pill", id: "ws-live" }, el("span", { class: `ws-dot ${streamOk ? "pass" : "warn"}` }), streamOk ? "Live updates" : "Connecting"),
        el("span", { class: "ws-pill" }, PAGE_VERSION === s.version ? `Version ${s.version}` : el("b", { text: `This page is ${PAGE_VERSION}; the app is ${s.version}. Reload.` })));
      document.getElementById("ws-status").replaceChildren(...parts);
    } catch (e) {
      document.getElementById("ws-status").replaceChildren(el("span", { class: "ws-pill" }, el("span", { class: "ws-dot fail" }), e.message));
    }
  }

  let streamOk = false;
  async function stream() {
    let delay = 1000;
    try { const d = await api("/v3/events"); lastEvent = d.latest || 0; streamOk = true; markLive(); } catch (e) { /* the loop retries */ }
    for (;;) {
      try {
        const d = await api(`/v3/events?after=${lastEvent}&wait=20`);
        if (!streamOk) { streamOk = true; markLive(); }
        delay = 1000;
        for (const ev of d.events) {
          lastEvent = Math.max(lastEvent, ev.id);
          for (const [prefix, fn] of eventHandlers) if (ev.topic.startsWith(prefix)) { try { fn(ev); } catch (e) { console.error(e); } }
        }
        if (!d.events.length) lastEvent = Math.max(lastEvent, d.latest || 0);
      } catch (e) {
        if (streamOk) { streamOk = false; markLive(); }
        await new Promise(r => setTimeout(r, delay));
        delay = Math.min(30000, delay * 2);
      }
    }
  }
  function markLive() {
    const p = document.getElementById("ws-live");
    if (p) p.replaceChildren(el("span", { class: `ws-dot ${streamOk ? "pass" : "warn"}` }), streamOk ? "Live updates" : "Reconnecting");
  }
  function onEvent(prefix, fn) { eventHandlers.push([prefix, fn]); }

  /* ---------- the classic screens inside the workstation ---------- */
  function wireClassic() {
    // A classic button that opens another screen opens it as a widget here.
    state.route = name => routeClassic(name);
    // "Analyze" anywhere opens Research on that stock, with the direction it was found in.
    state.openDecide = (symbol, direction) => {
      const t = goWorkspace("research");
      const linked = [...t.widgets.values()].find(w => w.group !== "none");
      set(linked ? linked.group : "blue", { symbol, direction: direction || null });
    };
  }

  /* ---------- boot ---------- */
  async function boot() {
    document.getElementById("ws-palette-btn").addEventListener("click", () => openPalette());
    document.getElementById("ws-add").addEventListener("click", e => widgetMenu(e.currentTarget));
    document.getElementById("ws-layout").addEventListener("click", e => layoutMenu(e.currentTarget));
    document.getElementById("ws-classic").addEventListener("click", e => classicMenu(e.currentTarget));
    document.getElementById("ws-dock-toggle").addEventListener("click", () => setDockOpen(!dockOpen));
    document.getElementById("ws-palette").addEventListener("pointerdown", e => { if (e.target.id === "ws-palette") closePalette(); });
    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
      if (e.key === "Escape") { const m = document.querySelector(".grid-stack-item.ws-max"); if (m) { const w = allWidgets().find(x => x.node.closest(".grid-stack-item") === m); if (w) w.toggleMax(); } closeMenu(); }
    });
    await refreshStatus();
    wireClassic();
    const [listing, syms] = await Promise.all([api("/v3/workspaces").catch(() => ({ session: {} })), api("/v3/symbols").catch(() => ({ symbols: [] }))]);
    symbolsList = syms.symbols || [];
    const session = listing.session || {};
    restoring = true;
    for (const [g, c] of Object.entries(session.context || {})) if (ctx[g]) Object.assign(ctx[g], c);
    if (!ctx.blue.symbol) ctx.blue.symbol = (syms.watchlist || [])[0] || "AAPL";
    const saved = (session.tabs || []).filter(t => WORKSPACES[t.workspace]);
    if (saved.length) for (const t of saved) openTab(t, false);
    else for (const k of Object.keys(WORKSPACES)) {
      const def = (listing.layouts || []).find(l => l.workspace === k && l.is_default);
      openTab(def ? { workspace: k, name: def.name, layoutId: def.id, widgets: def.widgets } : { workspace: k, name: WORKSPACES[k], widgets: WS.DEFAULT_LAYOUTS[k] || [] }, false);
    }
    const dock = session.dock && session.dock.widget !== undefined ? session.dock : { widget: WS.DEFAULT_DOCK, open: true };
    mountDock(dock.widget);
    setDockOpen(dock.open !== false);
    const want = location.hash.slice(1);
    const byHash = [...tabs.values()].find(t => t.workspace === want);
    activate(byHash ? byHash.id : (tabs.has(session.active) ? session.active : tabs.keys().next().value));
    restoring = false;
    booted = true;
    // A change made in the last moment before the page closes is still saved.
    window.addEventListener("pagehide", () => {
      if (!saveTimer) return;
      try { fetch("/v3/workspaces/session", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify(sessionBody()) }); }
      catch (e) { /* the page is going */ }
    });
    let resizeTimer = null;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { for (const t of tabs.values()) applyColumns(t); }, 150); });
    setInterval(refreshStatus, 30000);
    onEvent("desk.", () => refreshStatus());
    onEvent("proposal.", () => refreshStatus());
    onEvent("daemon.", () => refreshStatus());
    stream();
  }

  return { GROUPS, WORKSPACES, register, set, contextOf, provide, find, empty, toast, openMenu, closeMenu, addToActive, goWorkspace,
    onEvent, boot, saveNow, routeClassic, allWidgets, tabs: () => tabs, active: () => activeTab, ctx, catalog, openPalette, saveLayout,
    status: () => statusInfo, DEFAULT_LAYOUTS: {}, DEFAULT_DOCK: null };
})();
