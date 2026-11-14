/* chart.js: the interactive chart on Analyze opportunity (2.12; then "Analyze a stock").
   Bars within the day or grouped by day, week, month or quarter; any range, custom dates included; zoom and pan
   (drag, wheel, pinch); indicators switched on and off; swings, levels and trendlines; where a top or bottom may
   be forming; the news on the bars; similar patterns from this stock's own past overlaid on the window with what
   followed them; and the possible outcomes with how often each happened. Drawn with Lightweight Charts
   (lwcharts.js, Apache-2.0, served by the app). Nothing here places an order. */

const CHART_RANGES = [["1d", "1D"], ["5d", "5D"], ["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["1y", "1Y"], ["2y", "2Y"], ["5y", "5Y"], ["10y", "10Y"], ["custom", "Custom"]];
const CHART_GROUPS = [["1m", "1 min"], ["5m", "5 min"], ["15m", "15 min"], ["1h", "1 hour"], ["1d", "Day"], ["1w", "Week"], ["1M", "Month"], ["1Q", "Quarter"]];
const CHART_TOGGLES = [
  ["candles", "Candles"], ["volume", "Volume"], ["ema20", "20-bar EMA"], ["sma50", "50-bar average"], ["sma200", "200-bar average"],
  ["bollinger", "Bollinger bands"], ["vwap", "VWAP"], ["levels", "Support & resistance"], ["swings", "Swings"], ["trend", "Trendlines"],
  ["reversal", "Tops & bottoms forming"], ["news", "News"], ["macd", "MACD"], ["rsi", "RSI"], ["overlay", "Pattern overlay"]];
const CHART_DEFAULTS = { candles: true, volume: true, ema20: false, sma50: true, sma200: true, bollinger: false, vwap: false,
  levels: true, swings: true, trend: true, reversal: true, news: true, macd: true, rsi: false, overlay: true };

function chartColors() {
  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  return { bg: v("--panel"), ink: v("--ink"), muted: v("--muted"), line: v("--line"), accent: v("--accent"),
    up: v("--pass"), down: v("--fail"), warn: v("--warn"), info: v("--info"),
    s1: v("--series-1"), s2: v("--series-2"), s3: v("--series-3"), s4: v("--series-4") };
}

const chartClock = t => NY_CLOCK.format(new Date(t * 1000));
const chartDay = t => new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", year: "numeric" }).format(new Date(t * 1000));
const chartWhen = (t, intraday) => intraday
  ? new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(t * 1000))
  : chartDay(t);

/* Pure helpers (tested under Node): the bars a window covers, the lines a pattern overlay needs, the scenario rows. */
const CHARTX = (() => {
  function clampWindow(from, to, count, limit) {
    let a = Math.max(0, Math.floor(from)), b = Math.min(count - 1, Math.ceil(to));
    if (b < a) [a, b] = [b, a];
    if (b - a + 1 > limit) a = b - limit + 1;
    return [a, b];
  }
  function overlayLines(sim, show) {
    const out = [];
    if (!sim || !sim.matches) return out;
    const times = sim.window_times || [], future = sim.future_times || [];
    const lastClose = sim.overlay ? sim.overlay.from_close : null;
    sim.matches.slice(0, show).forEach((m, k) => {
      const before = m.fit.map((p, i) => ({ time: times[i], value: p })).filter(x => x.time !== undefined);
      const after = (m.after || []).map((r, i) => ({ time: future[i], value: lastClose ? lastClose * (1 + r) : null }))
        .filter(x => x.time !== undefined && x.value !== null);
      // Times must be unique and ascending (the library's production build does not check, and mis-indexes
      // otherwise): the match's own last point stands where the window ends, and the path after follows it.
      out.push({ kind: "match", index: k, points: before.concat(after) });
    });
    if (sim.overlay) {
      for (const key of ["median", "p25", "p75"]) {
        const pts = sim.overlay[key].map((p, i) => ({ time: future[i], value: p })).filter(x => x.time !== undefined && x.value !== null);
        if (pts.length) out.push({ kind: key, points: [{ time: times[times.length - 1], value: lastClose }].concat(pts) });
      }
    }
    return out;
  }
  function scenarioRows(h) {
    if (!h) return [];
    const rows = [];
    if (h.scenarios) {
      for (const key of ["up_range", "within", "down_range"]) {
        const s = h.scenarios[key];
        rows.push({ key, label: s.label, pct: 100 * s.frequency, low: 100 * s.low, high: 100 * s.high, cases: s.cases, of: h.cases });
      }
    }
    rows.push({ key: "up", label: "Higher at all", pct: 100 * h.up_frequency, low: 100 * h.low, high: 100 * h.high, cases: h.up, of: h.cases });
    return rows;
  }
  // 2.14: vertical zoom arithmetic. A pinch's distance and middle; the price to keep still (the one under the
  // fingers, clamped to the range); the narrowed or widened range around it.
  function pinchOf(touches) {
    const a = touches[0], b = touches[1];
    return { dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2,
             dx: Math.abs(a.clientX - b.clientX), dy: Math.abs(a.clientY - b.clientY) };
  }
  // 4.6.1: a two-finger pinch zooms each axis by how far the fingers moved apart along it: spread them sideways for
  // time, up and down for price, diagonally for both. An axis the fingers barely span (under 40 px) is left alone,
  // so a sideways pinch does not jitter the price axis.
  function pinchFactors(prev, now, minSpan = 40) {
    const f = (a, b) => (a >= minSpan && b >= minSpan ? b / a : 1);
    return { fx: f(prev.dx, now.dx), fy: f(prev.dy, now.dy) };
  }
  // The visible bars narrowed (factor > 1) or widened around the bar at `anchor`, kept between minBars and maxBars wide.
  function zoomLogical(range, factor, anchor, minBars = 5, maxBars = 5000) {
    if (!range || !(factor > 0)) return range;
    const width = range.to - range.from;
    const w = Math.min(maxBars, Math.max(minBars, width / Math.min(4, Math.max(0.25, factor))));
    const a = typeof anchor === "number" && isFinite(anchor) ? Math.min(range.to, Math.max(range.from, anchor)) : (range.from + range.to) / 2;
    const left = width > 0 ? (a - range.from) / width : 0.5;
    return { from: a - left * w, to: a + (1 - left) * w };
  }
  function zoomCentre(price, range) {
    if (typeof price !== "number" || !isFinite(price)) return (range.min + range.max) / 2;
    return Math.min(range.max, Math.max(range.min, price));
  }
  function zoomRange(range, factor, centre) {
    const f = Math.min(4, Math.max(0.25, factor));
    const min = centre - (centre - range.min) / f, max = centre + (range.max - centre) / f;
    const floor = Math.max(1e-6, centre * 0.002);
    if (max - min < floor) return range;                                 // zoomed in as far as is readable
    return { min, max };
  }
  return { clampWindow, overlayLines, scenarioRows, pinchOf, pinchFactors, zoomLogical, zoomCentre, zoomRange };
})();

// The Analyze screen's live chart: a new analysis disposes the last (v2.js). 3.0: a chart no longer disposes others
// itself, so the workstation can show several at once; whoever makes one disposes it.
let CHART_ACTIVE = null;

function interactiveChart(symbol) {
  const lib = window.LightweightCharts;
  const state_ = { range: "6m", group: null, start: "", end: "", show: { ...CHART_DEFAULTS }, swing: "2",
    data: null, sim: null, selecting: false, selection: null, userWindow: false, request: 0, minCorr: 0.75, horizon: null, matchesShown: 5,
    view: null,                                // the logical range to show after a draw, when not the range
    lines: {},                                 // 2.13: lines other widgets draw on the chart (the what-if paths), by key
    vzoom: null,                               // 2.14: the price range shown when zoomed vertically (null = automatic)
    hidden: new Set(),                         // 4.6.1: legend lines switched off, by key (4.7: by owner and key)
    notices: {} };                             // 4.7: why an owner's lines are not drawn (a forecast on intraday bars), by owner
  const listeners = [];                        // 2.13: told when the data or the pattern search changes
  const chartBox = el("div", { class: "lw-chart" });
  const readout = el("div", { class: "small muted lw-readout", text: "Hover the chart for the bar's figures. Drag to pan, scroll to zoom time, pinch to zoom; drag the price axis to stretch prices." });
  // 4.6.1: lines drawn with a `legend` label are named here, not on the price axis, so the scenarios do not pile up
  // labels on the right. Click an entry to hide or show its line.
  const legendBox = el("div", { class: "lw-legend", role: "list", "aria-label": "Lines on the chart" });
  // 4.7: a line drawn for a study carries its owner (`ns`, the study), so its visibility is that study's alone.
  const hk = ln => (ln.ns ? `${ln.ns}|${ln.key}` : ln.key);
  const noticeBox = el("div", { class: "lw-notice", role: "status", hidden: true });
  const info = el("div", { class: "small lw-info" });
  const notes = el("div");
  const err = el("div", { class: "error", role: "alert" });
  const outlookBox = el("div", { class: "panel lw-outlook" }, el("h3", { text: "Possible outcomes" }), el("p", { class: "muted small", text: "Press Find similar patterns to read the window on the screen against this stock's own past." }));
  const patternBox = el("div", { class: "panel lw-patterns" }, el("h3", { text: "Similar patterns" }), el("p", { class: "muted small", text: "Nothing searched yet." }));
  const turnBox = el("div", { class: "panel lw-turns" }, el("h3", { text: "Top or bottom forming?" }), el("p", { class: "muted small", text: "Loading…" }));
  const structBox = el("div", { class: "panel" }, el("h3", { text: "Price structure" }), el("p", { class: "muted", text: "Loading…" }));

  // --- controls ---------------------------------------------------------------------------------------------
  const rangeSeg = segmented(`crange${++uid}`, CHART_RANGES, state_.range);
  const groupSel = el("select", { "aria-label": "Group bars by" }, CHART_GROUPS.map(([v, t]) => el("option", { value: v, text: t })));
  const startIn = el("input", { type: "date", "aria-label": "Custom range start" });
  const endIn = el("input", { type: "date", "aria-label": "Custom range end" });
  const customBox = el("span", { class: "lw-custom hidden" }, startIn, " to ", endIn, el("button", { class: "plain small-btn", text: "Show" }));
  const swingSel = el("select", { "aria-label": "Swing size" }, [["1.5", "Small swings (1.5 ATR)"], ["2", "Medium swings (2 ATR)"], ["3", "Large swings (3 ATR)"]].map(([v, t]) => el("option", { value: v, text: t, selected: v === state_.swing })));
  const fitBtn = el("button", { class: "plain small-btn", text: "Fit range" });
  const latestBtn = el("button", { class: "plain small-btn", text: "Latest" });
  const toggles = CHART_TOGGLES.map(([key, label]) => {
    const box = el("input", { type: "checkbox", checked: state_.show[key] });
    box.addEventListener("change", () => { state_.show[key] = box.checked; draw(); });
    return el("label", { class: "check", "data-toggle": key }, box, label);
  });
  const findBtn = el("button", { class: "primary small-btn", text: "Find similar patterns" });
  const selectBtn = el("button", { class: "plain small-btn", text: "Select a window on the chart" });
  const corrIn = el("input", { type: "range", min: "0.5", max: "0.95", step: "0.05", value: String(state_.minCorr), "aria-label": "Minimum correlation" });
  const corrOut = el("span", { class: "small muted", text: "75%" });
  corrIn.addEventListener("input", () => { state_.minCorr = Number(corrIn.value); corrOut.textContent = `${Math.round(state_.minCorr * 100)}%`; });

  rangeSeg.addEventListener("change", () => {
    state_.range = rangeSeg.value();
    customBox.classList.toggle("hidden", state_.range !== "custom");
    state_.group = null;                       // the range picks its own grouping until the user chooses one
    if (state_.range !== "custom") load();
  });
  customBox.querySelector("button").addEventListener("click", () => { state_.start = startIn.value; state_.end = endIn.value; load(); });
  groupSel.addEventListener("change", () => { state_.group = groupSel.value; load(); });
  swingSel.addEventListener("change", () => { state_.swing = swingSel.value; load(); loadStructure(); });

  // --- the chart ---------------------------------------------------------------------------------------------
  let chart = null, series = {}, markers = null, priceLines = [], overlaySeries = [], selectionBox = null, lineSeries = [];
  const colors = () => chartColors();
  function makeChart() {
    if (chart) { chart.remove(); chart = null; }
    const c = colors();
    chart = lib.createChart(chartBox, {
      autoSize: true,
      layout: { background: { type: "solid", color: c.bg }, textColor: c.ink, attributionLogo: false, panes: { separatorColor: c.line, enableResize: true } },
      grid: { vertLines: { color: c.line }, horzLines: { color: c.line } },
      crosshair: { mode: lib.CrosshairMode.Normal },
      rightPriceScale: { borderColor: c.line },
      timeScale: { borderColor: c.line, timeVisible: false, secondsVisible: false, rightOffset: 4, shiftVisibleRangeOnNewBar: false },
      handleScroll: true, handleScale: true,
    });
    chart.subscribeCrosshairMove(onHover);
    chart.subscribeClick(onClick);
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => placeSelection());
    chart.timeScale().subscribeVisibleTimeRangeChange(() => placeSelection());
  }
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onTheme = () => { if (state_.data && panel.isConnected) draw(); };
  mq.addEventListener("change", onTheme);
  const dispose = () => { mq.removeEventListener("change", onTheme); if (chart) { chart.remove(); chart = null; } state_.data = null; };

  function bar(i) { return state_.data.bars[i]; }
  function indexOfTime(tl) {
    const bars = state_.data.bars;
    let lo = 0, hi = bars.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].tl < tl) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function draw() {
    const d = state_.data;
    if (!d) return;
    makeChart();
    series = {}; priceLines = []; overlaySeries = []; markers = null;
    const c = colors();
    const intraday = d.group.kind === "intraday";
    chart.timeScale().applyOptions({ timeVisible: intraday });
    const bars = d.bars;
    const show = state_.show;
    const price = show.candles
      ? chart.addSeries(lib.CandlestickSeries, zoomed({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down }))
      : chart.addSeries(lib.LineSeries, zoomed({ color: c.accent, lineWidth: 2, priceLineVisible: true }));
    price.setData(bars.map(b => show.candles ? { time: b.tl, open: b.open, high: b.high, low: b.low, close: b.close } : { time: b.tl, value: b.close }));
    series.price = price;
    if (show.volume) {
      const vol = chart.addSeries(lib.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      vol.setData(bars.map(b => ({ time: b.tl, value: b.volume || 0, color: (b.close >= b.open ? c.up : c.down) + "66" })));
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      series.volume = vol;
    }
    const line = (key, color, width = 1, style = 0) => {
      const s = chart.addSeries(lib.LineSeries, zoomed({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }));
      s.setData(bars.filter(b => isNum(b[key])).map(b => ({ time: b.tl, value: b[key] })));
      series[key] = s;
    };
    if (show.ema20) line("ema20", c.s3);
    if (show.sma50) line("sma50", c.s1);
    if (show.sma200) line("sma200", c.s2);
    if (show.vwap && intraday) line("vwap", c.s4, 1, 2);
    if (show.bollinger) { line("bb_high", c.muted, 1, 1); line("bb_low", c.muted, 1, 1); line("bb_mid", c.muted, 1, 3); }
    // Levels and trendlines.
    const st = d.structure;
    if (st && show.levels) {
      for (const side of ["support", "resistance"]) {
        for (const lv of st.levels[side] || []) {
          priceLines.push(price.createPriceLine({ price: lv.price, color: side === "support" ? c.up : c.down, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `${side === "support" ? "S" : "R"} ×${lv.touches}` }));
        }
      }
    }
    if (st && show.trend) {
      for (const ln of st.trendlines || []) {
        const s = chart.addSeries(lib.LineSeries, zoomed({ color: ln.crossed ? c.warn : c.accent, lineWidth: 1, lineStyle: ln.crossed ? 1 : 0, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }));
        s.setData([{ time: ln.tl_from, value: ln.from_price }, { time: ln.tl_to, value: ln.value_now }]);
      }
    }
    // Markers: swings, reversal stages, news.
    const marks = [];
    if (st && show.swings) {
      for (const sw of st.swings || []) {
        marks.push({ time: sw.tl, position: sw.kind === "high" ? "aboveBar" : "belowBar", color: c.muted, shape: "circle", size: 0.6, text: sw.label || (sw.kind === "high" ? "H" : "L"), _what: { kind: "swing", sw } });
      }
    }
    if (d.reversal && show.reversal) {
      for (const m of d.reversal.markers || []) {
        const bottom = m.side === "bottom";
        marks.push({ time: m.tl, position: bottom ? "belowBar" : "aboveBar", color: m.stage === "FAILED" ? c.muted : bottom ? c.up : c.down,
          shape: m.stage === "CONFIRMED" ? (bottom ? "arrowUp" : "arrowDown") : "square", size: m.stage === "CONFIRMED" ? 1.2 : 0.9,
          text: `${bottom ? "Bottom" : "Top"}${m.stage === "CANDIDATE" ? "?" : m.stage === "CONFIRMED" ? " ✓" : " ✗"}`, _what: { kind: "reversal", m } });
      }
    }
    if (d.news && show.news) {
      for (const n of d.news.items || []) {
        marks.push({ time: n.tl, position: "aboveBar", color: c.info, shape: "circle", size: 0.8, text: "N", _what: { kind: "news", n } });
      }
    }
    marks.sort((a, b) => a.time - b.time);
    markers = marks;
    lib.createSeriesMarkers(price, marks.map(m => ({ time: m.time, position: m.position, color: m.color, shape: m.shape, size: m.size, text: m.text })));
    // Indicator panes.
    let pane = 1;
    if (show.macd) {
      const hist = chart.addSeries(lib.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
      hist.setData(bars.filter(b => isNum(b.macd_hist)).map(b => ({ time: b.tl, value: b.macd_hist, color: b.macd_hist >= 0 ? c.up + "99" : c.down + "99" })));
      const m = chart.addSeries(lib.LineSeries, { color: c.s1, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, pane);
      m.setData(bars.filter(b => isNum(b.macd)).map(b => ({ time: b.tl, value: b.macd })));
      const sg = chart.addSeries(lib.LineSeries, { color: c.s2, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, pane);
      sg.setData(bars.filter(b => isNum(b.macd_signal)).map(b => ({ time: b.tl, value: b.macd_signal })));
      chart.panes()[pane].setHeight(90);
      pane += 1;
    }
    if (show.rsi) {
      const r = chart.addSeries(lib.LineSeries, { color: c.s3, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, pane);
      r.setData(bars.filter(b => isNum(b.rsi)).map(b => ({ time: b.tl, value: b.rsi })));
      r.createPriceLine({ price: 70, color: c.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      r.createPriceLine({ price: 30, color: c.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      chart.panes()[pane].setHeight(80);
      pane += 1;
    }
    // The pattern overlay, then the lines other widgets asked for.
    if (show.overlay && state_.sim) drawOverlay();
    drawLines();
    // Show the range the request covered, or the view asked for. The range is set in the same tick the chart
    // is built, which the library lays out exactly; a range set on a chart already drawn is applied a frame
    // late and measured against the old data edge, so any change of view redraws the chart.
    const view = state_.view || { from: d.window.start - d.window.offset - 0.5, to: d.window.end - d.window.offset + 2 };
    chart.timeScale().setVisibleLogicalRange(view);
    requestAnimationFrame(placeSelection);
  }
  function showView(from, to) { state_.view = { from, to }; draw(); }
  function drawOverlay() {
    for (const s of overlaySeries) { try { chart.removeSeries(s); } catch (e) { /* already gone */ } }
    overlaySeries = [];
    const c = colors();
    for (const ln of CHARTX.overlayLines(state_.sim, state_.matchesShown)) {
      const style = ln.kind === "match" ? { color: c.s4 + "88", lineWidth: 1 } : ln.kind === "median" ? { color: c.accent, lineWidth: 2 } : { color: c.accent + "99", lineWidth: 1, lineStyle: 2 };
      const s = chart.addSeries(lib.LineSeries, zoomed({ ...style, priceLineVisible: false, lastValueVisible: ln.kind === "median", crosshairMarkerVisible: false, title: ln.kind === "median" ? "median after" : "" }));
      s.setData(ln.points);
      overlaySeries.push(s);
    }
  }
  function drawLines() {
    for (const s of lineSeries) { try { chart.removeSeries(s); } catch (e) { /* already gone */ } }
    lineSeries = [];
    if (!chart) return;
    for (const ln of Object.values(state_.lines)) {
      if (ln.noLine) continue;                 // 4.7: a legend note only (a case that is the base path)
      // Times unique and ascending: the library's production build does not check and mis-indexes otherwise.
      const pts = (ln.points || []).filter(p => p && isNum(p.time) && isNum(p.value)).sort((a, b) => a.time - b.time)
        .filter((p, i, arr) => i === 0 || p.time > arr[i - 1].time);
      if (pts.length < 2) continue;
      const inLegend = !!ln.legend;
      const s = chart.addSeries(lib.LineSeries, zoomed({ color: ln.color, lineWidth: ln.width || 2, lineStyle: ln.style || 0, priceLineVisible: false,
        lastValueVisible: !inLegend && !!ln.title, crosshairMarkerVisible: false, title: inLegend ? "" : ln.title || "",
        visible: !state_.hidden.has(hk(ln)) }));
      s.setData(pts);
      // 4.7: a marker on the line (the catalyst's session), only where its time is one of the line's points.
      const marks = (ln.markers || []).filter(m => pts.some(p => p.time === m.time));
      if (marks.length && lib.createSeriesMarkers) lib.createSeriesMarkers(s, marks.map(m => ({ time: m.time, position: m.position || "aboveBar",
        color: m.color || ln.color, shape: m.shape || "arrowDown", text: m.text || "" })));
      lineSeries.push(s);
    }
    drawLegend();
  }
  // 4.7: say why an owner's lines are not drawn, with the one action that fixes it (or nothing: null clears it).
  function setNotice(owner, n) {
    if (n) state_.notices[owner] = n; else delete state_.notices[owner];
    const all = Object.values(state_.notices);
    if (!all.length) { noticeBox.hidden = true; noticeBox.replaceChildren(); return; }
    noticeBox.hidden = false;
    noticeBox.replaceChildren(...all.map(x => el("p", { class: "small" }, x.text, " ",
      x.action ? el("button", { class: "plain small-btn", type: "button", onclick: () => { try { x.run(); } catch (e) { console.error(e); } } }, x.action) : "")));
  }
  // 4.7: the daily bars a session-level forecast is drawn on: the range is kept when it offers them, else six months.
  function useDaily() {
    const d = state_.data;
    if (!d) return;
    if (!(d.range.groups || []).includes("1d")) { state_.range = "6m"; rangeSeg.querySelectorAll("input").forEach(i => { i.checked = i.value === "6m"; }); customBox.classList.add("hidden"); }
    state_.group = "1d";
    load();
  }
  function drawLegend() {
    const items = Object.values(state_.lines).filter(ln => ln.legend && (ln.noLine || (ln.points || []).length >= 2));
    if (!items.length) { legendBox.replaceChildren(); legendBox.hidden = true; return; }
    if (legendBox.parentNode !== chartBox) chartBox.appendChild(legendBox);
    legendBox.hidden = false;
    // Lines that share a `legendShare` (a band's two edges) are one entry, switched together, showing both ends.
    const entries = [];
    for (const ln of items) {
      const share = ln.legendShare || ln.key;
      const e = entries.find(x => x.share === share);
      if (e) e.lines.push(ln); else entries.push({ share, lines: [ln], legend: ln.legend, group: ln.legendGroup || "", color: ln.color, style: ln.style });
    }
    const groups = [];
    for (const e of entries) {
      let row = groups.find(x => x.name === e.group);
      if (!row) groups.push(row = { name: e.group, items: [] });
      row.items.push(e);
    }
    legendBox.replaceChildren(...groups.map(g => el("div", { class: "lw-legend-group" },
      g.name ? el("span", { class: "lw-legend-head", text: g.name }) : "",
      ...g.items.map(en => {
        if (en.lines.every(ln => ln.noLine)) return el("span", { role: "listitem", class: "lw-legend-item lw-legend-note", title: en.lines[0].note || "" },
          el("span", { class: "lw-swatch dashed", style: `border-color: ${en.color}` }), el("span", { text: en.legend }));
        const off = en.lines.every(ln => state_.hidden.has(hk(ln)));
        const ends = en.lines.map(ln => ln.points[ln.points.length - 1]).map(p => p && p.value).filter(isNum).sort((a, b) => a - b);
        const value = !ends.length ? "" : ends.length === 1 ? money(ends[0]) : `${money(ends[0])}–${money(ends[ends.length - 1])}`;
        const b = el("button", { type: "button", role: "listitem", class: `lw-legend-item${off ? " off" : ""}`, "aria-pressed": off ? "false" : "true",
          title: `${off ? "Show" : "Hide"} ${en.legend}`, "data-line": en.share },
          el("span", { class: `lw-swatch${en.style ? " dashed" : ""}${en.style === 1 ? " dotted" : ""}`, style: `border-color: ${en.color}` }),
          el("span", { text: en.legend }),
          value ? el("span", { class: "muted", text: ` ${value}` }) : "");
        b.addEventListener("click", e => {
          e.stopPropagation();
          for (const ln of en.lines) { if (off) state_.hidden.delete(hk(ln)); else state_.hidden.add(hk(ln)); }
          // 4.7: the owner keeps the choice (a study's view preference); the chart only reflects it.
          const told = new Set();
          for (const ln of en.lines) if (typeof ln.onToggle === "function" && !told.has(ln.layer || ln.key)) {
            told.add(ln.layer || ln.key); try { ln.onToggle(off, ln); } catch (err) { console.error(err); } }
          drawLines();
        });
        return b;
      }))));
  }
  // 2.13: draw (or replace) a set of lines by key; `lines` is [{key, points:[{time,value}], color, width, style, title}].
  // Keys that start with `prefix` and are not in the new set are removed. Drawn in place, without a rebuild.
  function setLines(prefix, lines) {
    for (const k of Object.keys(state_.lines)) if (k.startsWith(prefix)) delete state_.lines[k];
    for (const ln of lines || []) {
      state_.lines[ln.key] = ln;
      // 4.7: an owner that keeps the visibility (a study's preferences) says it with each line it draws.
      if (typeof ln.visible === "boolean") { if (ln.visible) state_.hidden.delete(hk(ln)); else state_.hidden.add(hk(ln)); }
    }
    if (chart && state_.data) drawLines();
  }
  function notify(what) { for (const fn of listeners) { try { fn(what, chartArgs()); } catch (e) { console.error(e); } } }
  // 2.13: the chart's current question, for widgets that ask the server about the same window.
  function chartArgs() {
    const d = state_.data;
    if (!d) return null;
    const out = { symbol, range: state_.range, group: state_.group || d.group.key, start: state_.range === "custom" ? state_.start : null,
      end: state_.range === "custom" ? state_.end || null : null, min_correlation: state_.minCorr, horizon: state_.horizon,
      window_start: null, window_end: null, last_close: null, last_time: null, future_times: [], group_kind: d.group.kind, group_label: d.group.label };
    const sel = state_.sim ? [state_.sim.window.start, state_.sim.window.end] : state_.userWindow && state_.selection && state_.selection.length === 2
      ? [state_.selection[0] + d.window.offset, state_.selection[1] + d.window.offset] : null;
    if (sel) { out.window_start = sel[0]; out.window_end = sel[1]; }
    // The path starts at the window's last bar (a custom range can end in the past), never the newest bar loaded.
    const endBar = bar((sel ? sel[1] : d.window.end) - d.window.offset);
    if (endBar) { out.last_close = endBar.close; out.last_time = endBar.tl; }
    out.future_times = state_.sim && state_.sim.future_times ? state_.sim.future_times : d.future_times || [];
    out.sim = state_.sim;
    return out;
  }
  // 2.13: make sure `ahead` bars past the window's end are on the screen.
  function showAhead(ahead) {
    if (!chart || !state_.data) return;
    const d = state_.data;
    const endIdx = (state_.sim ? state_.sim.window.end : d.window.end) - d.window.offset;
    const vr = chart.timeScale().getVisibleLogicalRange();
    if (vr && vr.to >= endIdx + ahead + 1) return;
    const width = vr ? vr.to - vr.from : 130;
    const to = endIdx + ahead + 2;
    showView(Math.min(endIdx - 10, to - width), to);
  }
  // --- 2.14: vertical zoom. The library zooms time only; a pinch (two fingers, or ctrl+wheel from a trackpad)
  // also narrows the price range shown, around the price under the fingers. Every series on the main pane
  // answers the autoscale with that range while it is set; Fit range or a new load clears it.
  function zoomProvider(original) {
    const r = original();
    if (!state_.vzoom) return r;
    return { priceRange: { minValue: state_.vzoom.min, maxValue: state_.vzoom.max }, margins: r && r.margins ? r.margins : undefined };
  }
  function zoomed(options) { return { ...options, autoscaleInfoProvider: zoomProvider }; }
  function visiblePriceRange() {
    if (state_.vzoom) return state_.vzoom;
    // What is on the screen now: the price at the top and the bottom of the main pane.
    try {
      const h = chart.paneSize(0).height;
      const top = series.price.coordinateToPrice(0), bottom = series.price.coordinateToPrice(h);
      if (isNum(top) && isNum(bottom) && top > bottom) return { min: bottom, max: top };
    } catch (e) { /* fall through to the bars */ }
    const d = state_.data, vr = chart && chart.timeScale().getVisibleLogicalRange();
    if (!d || !vr) return null;
    const a = Math.max(0, Math.floor(vr.from)), b = Math.min(d.bars.length - 1, Math.ceil(vr.to));
    let lo = Infinity, hi = -Infinity;
    for (let i = a; i <= b; i++) { const x = d.bars[i]; if (x) { lo = Math.min(lo, x.low); hi = Math.max(hi, x.high); } }
    if (!isFinite(lo) || !isFinite(hi)) return null;
    const pad = (hi - lo) * 0.08 || hi * 0.01;
    return { min: lo - pad, max: hi + pad };
  }
  function applyVZoom() {
    if (!chart || !series.price) return;
    series.price.applyOptions({ autoscaleInfoProvider: zoomProvider });   // a changed option makes the pane autoscale again
    chart.priceScale("right").applyOptions({ autoScale: true });
  }
  // Zoom the price axis by `factor` (>1 narrows) keeping the price at pane coordinate `y` where it is.
  function zoomPrice(factor, y) {
    const range = visiblePriceRange();
    if (!range || !series.price || !(factor > 0)) return;
    const centre = CHARTX.zoomCentre(series.price.coordinateToPrice(y), range);
    state_.vzoom = CHARTX.zoomRange(range, factor, centre);
    applyVZoom();
  }
  function paneY(clientY) { return clientY - chartBox.getBoundingClientRect().top; }
  function paneX(clientX) { return clientX - chartBox.getBoundingClientRect().left; }
  // 4.6.1: the time axis zoomed by the same gestures, around the bar under the fingers (the library's own pinch
  // and ctrl+wheel zoom time only, and by a different amount, so they are handled here instead).
  function zoomTime(factor, x) {
    if (!chart || !state_.data || !(factor > 0) || factor === 1) return;
    const ts = chart.timeScale(), vr = ts.getVisibleLogicalRange();
    if (!vr) return;
    const anchor = ts.coordinateToLogical(x);
    const r = CHARTX.zoomLogical({ from: vr.from, to: vr.to }, factor, anchor, 5, Math.max(50, state_.data.bars.length * 2));
    if (r) ts.setVisibleLogicalRange(r);
  }
  function zoomBoth(fx, fy, clientX, clientY) {
    if (fx !== 1) zoomTime(fx, paneX(clientX));
    if (fy !== 1) zoomPrice(fy, paneY(clientY));
  }
  // Touch screens: two fingers zoom each axis by how they spread (see pinchFactors). Captured before the library
  // sees it, so the library's time-only pinch does not run as well; one finger still pans as before.
  let pinch = null;                                                       // the last two-finger position
  chartBox.addEventListener("touchstart", e => { pinch = e.touches.length === 2 ? CHARTX.pinchOf(e.touches) : null; }, { passive: true, capture: true });
  chartBox.addEventListener("touchmove", e => {
    if (e.touches.length !== 2) return;
    e.preventDefault(); e.stopPropagation();
    const now = CHARTX.pinchOf(e.touches);
    if (pinch) { const f = CHARTX.pinchFactors(pinch, now); zoomBoth(f.fx, f.fy, now.x, now.y); }
    pinch = now;
  }, { passive: false, capture: true });
  chartBox.addEventListener("touchend", e => { pinch = e.touches && e.touches.length === 2 ? CHARTX.pinchOf(e.touches) : null; }, { passive: true, capture: true });
  // A trackpad pinch in Chrome, Edge and Firefox arrives as a wheel with ctrlKey. It carries how much, not which way,
  // so it zooms both axes together, like magnifying the picture. A plain wheel still zooms time (the library).
  chartBox.addEventListener("wheel", e => {
    if (!e.ctrlKey || !state_.data) return;
    e.preventDefault(); e.stopPropagation();
    const dy = e.deltaY * (e.deltaMode === 1 ? 32 : e.deltaMode === 2 ? 120 : 1);   // lines and pages to pixels, as the library does
    const f = Math.exp(-dy * 0.01);
    zoomBoth(f, f, e.clientX, e.clientY);
  }, { passive: false, capture: true });
  // Safari on a Mac sends a trackpad pinch as gesture events (scale only): both axes together, as above.
  let gestureScale = 1;
  chartBox.addEventListener("gesturestart", e => { e.preventDefault(); gestureScale = 1; }, { passive: false });
  chartBox.addEventListener("gesturechange", e => {
    e.preventDefault();
    if (!state_.data || !(e.scale > 0)) return;
    const f = e.scale / gestureScale;
    gestureScale = e.scale;
    zoomBoth(f, f, e.clientX, e.clientY);
  }, { passive: false });
  chartBox.addEventListener("gestureend", e => { e.preventDefault(); gestureScale = 1; }, { passive: false });
  function showProjection(a, b) {
    // Show the window and what the matches did next, projected past the last bar.
    const ahead = state_.sim && state_.sim.matches.length ? (state_.sim.future_times || []).length : 0;
    const vr = chart ? chart.timeScale().getVisibleLogicalRange() : null;
    const width = vr ? vr.to - vr.from : 130;
    const to = b + ahead + 2;
    showView(Math.min(a - 3, to - width), to);
  }
  function placeSelection() {
    if (!chart) return;
    if (!selectionBox) { selectionBox = el("div", { class: "lw-selection hidden" }); chartBox.append(selectionBox); }
    const sel = state_.selection;
    if (!sel || !state_.data) { selectionBox.classList.add("hidden"); return; }
    const ts = chart.timeScale();
    const x1 = ts.timeToCoordinate(bar(sel[0]).tl), x2 = sel.length > 1 ? ts.timeToCoordinate(bar(sel[1]).tl) : x1;
    if (x1 === null && x2 === null) { selectionBox.classList.add("hidden"); return; }
    const left = Math.max(0, Math.min(x1 ?? 0, x2 ?? chartBox.clientWidth)), right = Math.min(chartBox.clientWidth - 60, Math.max(x1 ?? 0, x2 ?? chartBox.clientWidth));
    selectionBox.classList.remove("hidden");
    selectionBox.style.left = `${left}px`; selectionBox.style.width = `${Math.max(2, right - left)}px`;
  }
  // 3.1: other charts can follow this one's crosshair by day (and move it). A bar's `tl` is New York wall-clock time
  // written as UTC seconds, so its UTC date is its New York day.
  const crossFns = new Set();
  const dayOfTl = tl => new Date(tl * 1000).toISOString().slice(0, 10);
  let placing = false;
  function crosshairAt(day) {
    if (!chart || !series.price || !state_.data) return;
    placing = true;
    try {
      const hits = day ? state_.data.bars.filter(x => dayOfTl(x.tl) === day) : [];
      const b = hits[hits.length - 1];
      if (b) chart.setCrosshairPosition(b.close, b.tl, series.price); else chart.clearCrosshairPosition();
    } finally { placing = false; }
  }
  function onHover(param) {
    if (!placing) { const b0 = param && param.time && state_.data ? bar(indexOfTime(param.time)) : null; for (const fn of crossFns) { try { fn(b0 ? dayOfTl(b0.tl) : null); } catch (e) { console.error(e); } } }
    if (!param || !param.time || !state_.data) { return; }
    // 4.1: lines that carry a note for the hovered time (a scenario's price, move, session and trigger) say it too;
    // past the last bar there is no bar to describe, only them.
    const tips = [];
    // 4.7: a hidden line says nothing, so the readout matches what is on the screen.
    for (const ln of Object.values(state_.lines)) { if (state_.hidden.has(hk(ln))) continue; const t = ln.tips && ln.tips[param.time]; if (t) tips.push(t); }
    const lastBar = state_.data.bars[state_.data.bars.length - 1];
    if (lastBar && param.time > lastBar.tl) { if (tips.length) readout.textContent = tips.join("  ·  "); return; }
    const i = indexOfTime(param.time);
    const b = bar(i);
    if (!b) return;
    const intraday = state_.data.group.kind === "intraday";
    const parts = [`${chartWhen(b.tl, intraday)}${b.complete ? "" : " (forming)"}`, `O ${money(b.open)} H ${money(b.high)} L ${money(b.low)} C ${money(b.close)}`, `Vol ${num(b.volume, 0)}`];
    for (const [key, label] of [["ema20", "EMA20"], ["sma50", "SMA50"], ["sma200", "SMA200"], ["vwap", "VWAP"], ["rsi", "RSI"], ["macd", "MACD"], ["atr", "ATR"]]) {
      if (isNum(b[key]) && (state_.show[key] || key === "atr" || key === "rsi" && state_.show.rsi || key === "macd" && state_.show.macd)) parts.push(`${label} ${num(b[key], key === "rsi" ? 0 : 2)}`);
    }
    readout.textContent = parts.concat(tips).join("  ·  ");
  }
  function onClick(param) {
    if (!param || !param.time || !state_.data) return;
    const i = indexOfTime(param.time);
    if (state_.selecting) {
      if (state_.selection && state_.selection.length === 1) {
        const [a, b] = CHARTX.clampWindow(state_.selection[0], i, state_.data.bars.length, 400);
        state_.selection = [a, b]; state_.selecting = false; state_.userWindow = true;
        selectBtn.textContent = "Select a window on the chart";
        info.replaceChildren(el("span", { text: `Window selected: ${b - a + 1} bars, ${chartWhen(bar(a).tl, state_.data.group.kind === "intraday")} to ${chartWhen(bar(b).tl, state_.data.group.kind === "intraday")}. Press Find similar patterns.` }));
      } else { state_.selection = [i]; info.replaceChildren(el("span", { text: "Now click the last bar of the window." })); }
      placeSelection();
      return;
    }
    // What was clicked: the nearest marker on that bar.
    const hits = (markers || []).filter(m => m.time === param.time);
    if (!hits.length) return;
    const frag = el("div", { class: "lw-hit" });
    for (const m of hits) {
      const w = m._what;
      if (w.kind === "news") frag.append(el("div", {}, el("strong", { text: "News " }), w.n.url ? el("a", { href: w.n.url, target: "_blank", rel: "noopener noreferrer", text: w.n.headline }) : el("span", { text: w.n.headline }), el("span", { class: "muted", text: ` · ${w.n.source || ""}${w.n.via ? ` via ${w.n.via}` : ""} · ${clockTime(new Date(w.n.t * 1000).toISOString()).slice(0, 8)} New York${isNum(w.n.tone) ? ` · tone ${w.n.tone > 0 ? "positive" : w.n.tone < 0 ? "negative" : "neutral"}` : ""}` })));
      else if (w.kind === "swing") frag.append(el("div", {}, el("strong", { text: `Swing ${w.sw.kind} ` }), el("span", { text: `${money(w.sw.price)}, ${w.sw.label || ""}; confirmed ${w.sw.confirmed_date || chartDay(w.sw.tl_confirmed)}.` })));
      else if (w.kind === "reversal") frag.append(el("div", {}, el("strong", { text: `${w.m.side === "bottom" ? "Bottom" : "Top"} ${w.m.stage.toLowerCase()} ` }), el("span", { class: "muted", text: `at ${money(w.m.price)}. Stages read from that bar and earlier ones only.` })));
    }
    info.replaceChildren(frag);
  }
  fitBtn.addEventListener("click", () => { if (state_.data) { state_.view = null; state_.vzoom = null; draw(); } });
  latestBtn.addEventListener("click", () => { if (state_.data) { const n = state_.data.bars.length, vr = chart.timeScale().getVisibleLogicalRange(); showView(n - 1 - (vr ? vr.to - vr.from : 130) + 4, n + 3); } });
  selectBtn.addEventListener("click", () => {
    state_.selecting = !state_.selecting;
    if (state_.selecting) { state_.selection = null; state_.userWindow = false; selectBtn.textContent = "Cancel selecting"; info.replaceChildren(el("span", { text: "Click the first bar of the window." })); }
    else { state_.userWindow = false; selectBtn.textContent = "Select a window on the chart"; info.replaceChildren(); }
    placeSelection();
  });

  // --- loading -------------------------------------------------------------------------------------------------
  function query() {
    const p = new URLSearchParams({ range: state_.range, swing_atr: state_.swing });
    if (state_.group) p.set("group", state_.group);
    if (state_.range === "custom") { p.set("start", state_.start); if (state_.end) p.set("end", state_.end); }
    return p;
  }
  async function load() {
    const mine = ++state_.request;
    err.textContent = "";
    chartBox.classList.add("refetching");
    try {
      const d = await api(`/v2/chart/${encodeURIComponent(symbol)}?${query()}`);
      if (mine !== state_.request || !panel.isConnected) return;    // superseded, or the screen was replaced meanwhile
      state_.data = d; state_.sim = null; state_.selection = null; state_.userWindow = false; state_.view = null;
      state_.lines = {};                         // lines other widgets drew were for the old bars; they redraw their own
      state_.vzoom = null;
      state_.group = d.group.key;
      groupSel.replaceChildren(...CHART_GROUPS.filter(([v]) => d.range.groups.includes(v)).map(([v, t]) => el("option", { value: v, text: t, selected: v === d.group.key })));
      toggles.find(t => t.dataset.toggle === "vwap").classList.toggle("hidden", d.group.kind !== "intraday");
      draw();
      renderNotes(d);
      renderTurns(d);
      notify("load");
      patternBox.replaceChildren(el("h3", { text: "Similar patterns" }), el("p", { class: "muted small", text: `Press Find similar patterns to search this stock's ${d.history_bars.toLocaleString()} ${d.group.label} bars for the shape on the screen.` }));
      outlookBox.replaceChildren(el("h3", { text: "Possible outcomes" }), el("p", { class: "muted small", text: "The outcomes are read from the pattern search: press Find similar patterns." }));
    } catch (e) {
      if (mine !== state_.request) return;
      err.textContent = e.message;
    } finally { if (mine === state_.request) chartBox.classList.remove("refetching"); }
  }
  async function loadStructure() {
    try {
      const got = await api(`/api/technicals/${encodeURIComponent(symbol)}?bars=504&swing_atr=${state_.swing}`);
      structBox.replaceChildren(...structureView(got));
    } catch (e) { structBox.replaceChildren(el("h3", { text: "Price structure" }), el("p", { class: "error", text: e.message })); }
  }
  function renderNotes(d) {
    const items = [];
    for (const w of d.warnings || []) items.push(banner("warn", w));
    if (d.news && d.news.note) items.push(el("p", { class: "small muted", text: d.news.note }));
    if (d.source && d.source.source_note && d.group.kind === "intraday") items.push(el("p", { class: "small muted", text: d.source.source_note }));
    items.push(el("p", { class: "small muted", text: `${d.range.label}, ${d.group.label} bars: ${d.window.bars} in the range, ${d.bars.length.toLocaleString()} loaded to pan through${d.structure ? `; swings of ${d.structure.swing_atr} average ranges` : ""}.` }));
    notes.replaceChildren(...items);
  }
  function renderTurns(d) {
    const r = d.reversal;
    const kids = [el("h3", { text: "Top or bottom forming?" })];
    if (!r) kids.push(el("p", { class: "muted small", text: "Needs at least 80 bars of this grouping to read." }));
    else {
      for (const side of ["bottom", "top"]) {
        const s = r.now[side];
        const word = side === "bottom" ? "Bottom" : "Top";
        const stage = s.stage === "NONE" ? "no meaningful move to reverse" : s.stage.toLowerCase();
        const row = el("div", { class: "lw-turn" }, el("strong", { text: `${word}: ${stage}` }), el("span", { class: "muted small", text: ` · ${s.score} points` }));
        if (s.evidence && s.evidence.length) row.append(el("div", { class: "small muted", text: s.evidence.join("; ") }));
        if (s.levels) row.append(el("div", { class: "small", text: `Wrong below ${money(s.levels.invalidation)} (${s.levels.invalidation_is || ""}); pays at ${money(s.levels.target_1)} then ${money(s.levels.target_2)}.`.replace("Wrong below", side === "top" ? "Wrong above" : "Wrong below") }));
        kids.push(row);
      }
      kids.push(el("p", { class: "small muted", text: r.note }));
    }
    turnBox.replaceChildren(...kids);
  }
  async function findSimilar() {
    if (!state_.data || !chart) return;
    const d = state_.data;
    // The window: the one selected on the chart, else the last 40 bars on screen (each press reads the
    // screen afresh, so panning and pressing again searches the new view).
    let [a, b] = state_.userWindow ? state_.selection : (() => {
      const vr = chart.timeScale().getVisibleLogicalRange();
      return vr ? CHARTX.clampWindow(vr.from, vr.to, d.bars.length, 400) : [d.window.start - d.window.offset, d.window.end - d.window.offset];
    })();
    if (!state_.userWindow && b - a + 1 > 40) a = b - 39;
    findBtn.disabled = true;
    const p = query();
    p.set("window_start", String(a + d.window.offset)); p.set("window_end", String(b + d.window.offset));
    p.set("min_correlation", String(state_.minCorr));
    if (state_.horizon) p.set("horizon", String(state_.horizon));
    try {
      const o = await api(`/v2/chart/${encodeURIComponent(symbol)}/outlook?${p}`);   // one search: the outlook carries the matches
      const sim = o.similar;
      state_.sim = sim; state_.selection = [a, b];
      renderPatterns(sim); renderOutlook(o, sim);
      showProjection(a, b);
      notify("similar");
    } catch (e) { patternBox.replaceChildren(el("h3", { text: "Similar patterns" }), el("p", { class: "error", text: e.message })); }
    finally { findBtn.disabled = false; }
  }
  findBtn.addEventListener("click", findSimilar);
  function renderPatterns(sim) {
    const intraday = state_.data.group.kind === "intraday";
    const kids = [el("h3", { text: `Similar patterns (${sim.matches.length})` }), el("p", { class: "small muted", text: sim.note || "" })];
    if (sim.matches.length) {
      const shown = el("select", {}, [3, 5, 10, 20].map(n => el("option", { value: String(n), text: `Overlay the best ${n}`, selected: n === state_.matchesShown })));
      shown.addEventListener("change", () => { state_.matchesShown = Number(shown.value); if (state_.show.overlay) draw(); });
      kids.push(el("div", { class: "row" }, shown, el("span", { class: "small muted", text: " Thin lines: each match drawn over the window at its scale, then what followed it. Thick line: the median of what followed; dashed: the middle half." })));
      kids.push(table([
        { label: "When", cell: m => el("span", { text: `${chartWhen(m.tl_start, intraday)} – ${chartWhen(m.tl_end, intraday)}` }) },
        { label: "Likeness", num: true, cell: m => pct(100 * m.correlation, 0) },
        ...sim.horizons.slice(0, 3).map(h => ({ label: `${h.label} after`, num: true, cell: m => m.forward[String(h.bars)] ? el("span", { class: m.forward[String(h.bars)].pct >= 0 ? "pos-text" : "neg-text", text: signedPct(m.forward[String(h.bars)].pct, 1) }) : "n/a" })),
        { label: "", cell: m => { const b = el("button", { class: "plain small-btn", text: "Go there" }); b.addEventListener("click", () => showView(m.start - state_.data.window.offset - 5, m.end - state_.data.window.offset + 25)); return b; } },
      ], sim.matches));
    }
    patternBox.replaceChildren(...kids);
  }
  function renderOutlook(o, sim) {
    const kids = [el("h3", { text: "Possible outcomes" })];
    if (o.horizons && o.horizons.length) {
      const hsel = el("select", { "aria-label": "Horizon" }, o.horizons.map(h => el("option", { value: String(h.bars), text: `${h.label} on`, selected: o.horizon && h.bars === o.horizon.bars })));
      hsel.addEventListener("change", () => { state_.horizon = Number(hsel.value); findSimilar(); });   // one request
      kids.push(el("div", { class: "row" }, field("Horizon", hsel), el("span", { class: "small muted", text: `${o.matches} matches${o.price ? `; last ${money(o.price)}` : ""}` })));
      const rows = CHARTX.scenarioRows(o.horizon);
      kids.push(el("div", { class: "lw-scenarios" }, rows.map(r => el("div", { class: "lw-scenario" },
        el("div", { class: "lw-scenario-label", text: r.label }),
        el("div", { class: "lw-bar" }, el("i", { style: `width:${Math.max(1, r.pct).toFixed(0)}%` })),
        el("div", { class: "lw-scenario-pct" }, el("strong", { text: `${r.pct.toFixed(0)}%` }), el("span", { class: "muted small", text: ` (${r.low.toFixed(0)}–${r.high.toFixed(0)}%, ${r.cases} of ${r.of})` }))))));
    }
    const lean = o.lean || {};
    kids.push(el("p", {}, el("strong", { text: lean.direction === "no lean" ? "No lean" : `Leaning ${lean.direction}` }), el("span", { class: "muted", text: lean.of ? ` · ${lean.for} of the ${lean.of} reading${lean.of === 1 ? "" : "s"} that count${lean.why && lean.why.length ? `: ${lean.why.join(", ")}` : ""}.` : " · no reading has ten cases and a way that followed at least 55% of the time." })));
    kids.push(el("ul", { class: "plain small lw-readings" }, (o.readings || []).map(r => el("li", {},
      el("strong", { text: `${r.label}: ` }),
      isNum(r.frequency) ? el("span", { class: r.direction > 0 ? "pos-text" : r.direction < 0 ? "neg-text" : "", text: `${(100 * r.frequency).toFixed(0)}%` }) : "",
      isNum(r.frequency) && isNum(r.low) ? el("span", { class: "muted", text: ` (${(100 * r.low).toFixed(0)}–${(100 * r.high).toFixed(0)}%, ${r.cases} cases${r.enough ? "" : ", too few to lean on"}) ` }) : " ",
      el("span", { text: r.text || "" }),
      r.items && r.items.length ? el("ul", { class: "plain small" }, r.items.slice(0, 3).map(n => el("li", {}, n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener noreferrer", text: n.headline }) : el("span", { text: n.headline }), el("span", { class: "muted", text: ` · ${n.source || ""}` })))) : ""))));
    kids.push(el("p", { class: "small muted", text: o.note }));
    outlookBox.replaceChildren(...kids);
  }

  const panel = el("div", { class: "panel lw-panel" },
    el("h3", { text: "Chart" }),
    el("div", { class: "row chart-controls" }, field("Range", rangeSeg), customBox, field("Group by", groupSel), swingSel, fitBtn, latestBtn),
    el("div", { class: "toggles" }, toggles),
    err, noticeBox, chartBox, readout, info,
    el("div", { class: "row lw-find" }, findBtn, selectBtn, el("label", { class: "check" }, "Likeness at least ", corrIn, corrOut)),
    notes);
  load();
  loadStructure();
  return { panel, extras: [outlookBox, patternBox, turnBox], structure: structBox, dispose,
    setLines, chartArgs, showAhead, subscribe: fn => { listeners.push(fn); }, redraw: () => { if (state_.data) draw(); },
    notice: setNotice, useDaily,
    // 4.7: whether a line is shown (its owner's and key), for the widgets and the tests that check them.
    shown: (key, ns) => { const ln = state_.lines[key]; return !!ln && !state_.hidden.has(ns ? `${ns}|${key}` : hk(ln)); },
    readout: () => readout.textContent, hover: time => onHover({ time }),
    // 4.1: the lines drawn by key with their points, read-only (a copy), for the widgets and the tests that check them.
    lines: prefix => JSON.parse(JSON.stringify(Object.fromEntries(Object.entries(state_.lines).filter(([k]) => !prefix || k.startsWith(prefix))))),
    crosshair: { on: fn => { crossFns.add(fn); return () => crossFns.delete(fn); }, set: crosshairAt } };
}
