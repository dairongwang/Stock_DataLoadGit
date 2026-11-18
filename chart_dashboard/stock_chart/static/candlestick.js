/*
 * candlestick.js - a small, dependency-free SVG candlestick chart with a volume pane.
 *
 *   const chart = new Candlestick.CandleChart(element, {onChange: (summary) => ...});
 *   chart.setData(rows, "6M");     // rows: [[date "YYYY-MM-DD", open, high, low, close, volume], ...] oldest first
 *   chart.setData(rows, "5Y", "weekly");   // one candle per ISO week / calendar month: the row date is the first
 *                                  // day of the period, followed by [first trade, last trade, trading days, complete]
 *   chart.setRange("1Y");          // 1M 3M 6M YTD 1Y 5Y ALL
 *   chart.setMovingAverage(20, true);
 *
 * Interaction: hover = crosshair + values of the candle, mouse wheel = zoom, drag = pan,
 * double click = back to the selected range.  The x axis is trading days / weeks / months
 * (no gaps for weekends / holidays).  Up candles (close >= open) are green, down candles red;
 * the volume bars take the colour of their candle.
 *
 * The pure helpers (niceTicks, formatPrice, formatVolume, sma, windowForRange, summarize,
 * timeLabels, isoWeek, periodLabel) are exported for unit tests (node: require("./candlestick.js")).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.Candlestick = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var D = 0, O = 1, H = 2, L = 3, C = 4, V = 5;           // column indexes of a row
  var FIRST = 6, LAST = 7, DAYS = 8, COMPLETE = 9;        // ... and the extra ones of a weekly / monthly row
  var UNITS = {daily: ["trading day", "trading days"], weekly: ["week", "weeks"], monthly: ["month", "months"]};
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var RANGE_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "5Y": 60};

  // ---------------------------------------------------------------- helpers
  function niceStep(span, count) {
    if (!(span > 0)) { return 1; }
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  /** Round tick values covering [min, max]: {step, ticks}. */
  function niceTicks(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) { return {step: 1, ticks: []}; }
    if (min === max) { min -= 1; max += 1; }
    var step = niceStep(max - min, count || 5);
    var first = Math.ceil(min / step - 1e-9) * step;
    var ticks = [];
    for (var v = first; v <= max + step * 1e-9; v += step) {
      ticks.push(Number(v.toFixed(10)));
      if (ticks.length > 50) { break; }
    }
    return {step: step, ticks: ticks};
  }

  function decimalsFor(value) {
    var a = Math.abs(value);
    return a >= 1000 ? 2 : a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
  }

  /** 1234.5 -> "1,234.50"; 0.05123 -> "0.0512" */
  function formatPrice(value, decimals) {
    if (value === null || value === undefined || !isFinite(value)) { return "-"; }
    var d = decimals === undefined ? decimalsFor(value) : decimals;
    return Number(value).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  /** 19632000 -> "19.63M" */
  function formatVolume(value) {
    if (value === null || value === undefined || !isFinite(value)) { return "-"; }
    var a = Math.abs(value);
    var units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var n = value / units[i][0];
        return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + units[i][1];
      }
    }
    return String(Math.round(value));
  }

  function formatPercent(value) {
    if (value === null || value === undefined || !isFinite(value)) { return "-"; }
    return (value > 0 ? "+" : "") + value.toFixed(2) + "%";
  }

  /** Simple moving average of the close: one value per row, null until *period* rows exist. */
  function sma(rows, period) {
    var out = new Array(rows.length), sum = 0;
    for (var i = 0; i < rows.length; i++) {
      sum += rows[i][C];
      if (i >= period) { sum -= rows[i - period][C]; }
      out[i] = i >= period - 1 ? sum / period : null;
    }
    return out;
  }

  function parseDate(text) {
    return new Date(Date.UTC(+text.slice(0, 4), +text.slice(5, 7) - 1, +text.slice(8, 10)));
  }

  function isoDate(d) {
    return d.toISOString().slice(0, 10);
  }

  function addMonths(text, months) {
    var d = parseDate(text);
    var day = d.getUTCDate();
    d.setUTCDate(1);
    d.setUTCMonth(d.getUTCMonth() + months);
    var last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
    d.setUTCDate(Math.min(day, last));
    return isoDate(d);
  }

  /** The [start, end) row window of a range button, ending at the newest row. */
  function windowForRange(rows, range) {
    var n = rows.length;
    if (!n || range === "ALL") { return [0, n]; }
    var lastDate = rows[n - 1][D], cutoff;
    if (range === "YTD") {
      cutoff = lastDate.slice(0, 4) + "-01-01";
    } else {
      cutoff = addMonths(lastDate, -(RANGE_MONTHS[range] || 6));
    }
    var start = 0;
    while (start < n - 1 && rows[start][D] <= cutoff && range !== "YTD") { start++; }
    if (range === "YTD") {
      while (start < n - 1 && rows[start][D] < cutoff) { start++; }
    }
    return [start, n];
  }

  /** Figures of the rows in [start, end): open of the first day, close of the last, change, high, low, volume. */
  function summarize(rows, start, end) {
    if (!rows.length || end <= start) { return null; }
    var high = -Infinity, low = Infinity, volume = 0;
    for (var i = start; i < end; i++) {
      if (rows[i][H] > high) { high = rows[i][H]; }
      if (rows[i][L] < low) { low = rows[i][L]; }
      volume += rows[i][V];
    }
    var first = rows[start], last = rows[end - 1];
    var base = start > 0 ? rows[start - 1][C] : first[O];     // change against the previous close
    return {
      first: first[D], last: last[D], days: end - start, open: first[O], close: last[C],
      change: last[C] - base, changePct: base ? (last[C] - base) / base * 100 : null,
      high: high, low: low, volume: volume, avgVolume: volume / (end - start)
    };
  }

  /** One candle for rows[start..end): open of the first, highest high, lowest low, close of the last, summed volume. */
  function mergeRows(rows, start, end) {
    if (end - start === 1) { return rows[start]; }
    var high = -Infinity, low = Infinity, volume = 0;
    for (var i = start; i < end; i++) {
      if (rows[i][H] > high) { high = rows[i][H]; }
      if (rows[i][L] < low) { low = rows[i][L]; }
      volume += rows[i][V];
    }
    return [rows[start][D], rows[start][O], high, low, rows[end - 1][C], volume];
  }

  /** ISO 8601 week of a date: {year, week} (the week with the year's first Thursday is week 1). */
  function isoWeek(text) {
    var d = parseDate(text);
    var day = (d.getUTCDay() + 6) % 7;                    // Monday = 0
    d.setUTCDate(d.getUTCDate() - day + 3);               // the Thursday of the week decides the year
    var year = d.getUTCFullYear();
    var firstThursday = new Date(Date.UTC(year, 0, 4));
    firstThursday.setUTCDate(firstThursday.getUTCDate() - (firstThursday.getUTCDay() + 6) % 7 + 3);
    return {year: year, week: 1 + Math.round((d - firstThursday) / (7 * 864e5))};
  }

  function shortDate(text) {
    return MONTHS[+text.slice(5, 7) - 1] + " " + (+text.slice(8, 10));
  }

  /** The name of a candle: "2025-01-06" (daily), "Week 2, 2025" (weekly), "Jan 2025" (monthly);
   *  *long* adds the trading days a weekly / monthly candle spans. */
  function periodLabel(row, interval, long) {
    if (interval === "weekly") {
      var w = isoWeek(row[D]);
      var text = "Week " + w.week + ", " + w.year;
      if (long && row[FIRST]) {
        text += " (" + shortDate(row[FIRST]) + (row[LAST] && row[LAST] !== row[FIRST] ? " – " + shortDate(row[LAST]) : "") + ")";
      }
      return text;
    }
    if (interval === "monthly") {
      return MONTHS[+row[D].slice(5, 7) - 1] + " " + row[D].slice(0, 4);
    }
    return row[D];
  }

  /** Axis labels at month / quarter / year boundaries, at least *minGap* pixels apart.
   *  The step (1, 2, 3, 6 months; 1, 2, 5 years) follows from the pixels one month takes.
   *  Monthly candles are always labelled at month boundaries (never by week). */
  function timeLabels(rows, start, end, slot, minGap, interval) {
    minGap = minGap || 70;
    if (end <= start) { return []; }
    var first = rows[start][D], lastDay = rows[end - 1][D];
    var months = (+lastDay.slice(0, 4) - +first.slice(0, 4)) * 12 + (+lastDay.slice(5, 7) - +first.slice(5, 7)) + 1;
    var pxPerMonth = (end - start) * slot / Math.max(1, months);
    var steps = [1, 2, 3, 6, 12, 24, 60, 120], step = steps[steps.length - 1];
    for (var s = 0; s < steps.length; s++) {
      if (steps[s] * pxPerMonth >= minGap) { step = steps[s]; break; }
    }
    var labels = [], lastIndex = -Infinity;
    if (slot * 5 >= minGap && interval !== "monthly") {   // short periods: the first trading day of every week
      for (var w = start; w < end; w++) {
        var before = w > 0 ? parseDate(rows[w - 1][D]) : null, day = parseDate(rows[w][D]);
        var newWeek = !before || (day - before) / 864e5 >= 7 || day.getUTCDay() < before.getUTCDay();
        // the first label also names the year (it is longer): the next one keeps more distance
        var need = labels.length === 1 ? minGap * 1.25 : minGap * 0.8;
        if (!newWeek || (w - lastIndex) * slot < need) { continue; }
        var m = day.getUTCMonth();
        labels.push({index: w, text: (labels.length === 0 || day.getUTCDate() <= 7 ? MONTHS[m] + " " : "") +
                     day.getUTCDate() + (labels.length === 0 ? ", " + day.getUTCFullYear() : ""), major: day.getUTCDate() <= 7});
        lastIndex = w;
      }
      return labels;
    }
    for (var i = start; i < end; i++) {
      var prev = i > 0 ? rows[i - 1][D] : null, cur = rows[i][D];
      if (prev && prev.slice(0, 7) === cur.slice(0, 7)) { continue; }          // not the first day of a month
      var month = +cur.slice(5, 7) - 1, year = +cur.slice(0, 4);
      if ((year * 12 + month) % step !== 0) { continue; }
      if ((i - lastIndex) * slot < minGap * 0.8) { continue; }                  // irregular gaps in the data
      var label;
      if (step >= 12 || month === 0) {
        label = String(year);
      } else {
        label = labels.length === 0 ? MONTHS[month] + " " + year : MONTHS[month];
      }
      labels.push({index: i, text: label, major: month === 0});
      lastIndex = i;
    }
    return labels;
  }

  // ---------------------------------------------------------------- SVG helpers
  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    for (var k in attrs) { if (attrs[k] !== undefined && attrs[k] !== null) { node.setAttribute(k, attrs[k]); } }
    if (parent) { parent.appendChild(node); }
    return node;
  }

  function text(parent, x, y, value, attrs) {
    var node = el("text", Object.assign({x: x, y: y}, attrs || {}), parent);
    node.textContent = value;
    return node;
  }

  function px(v) { return Math.round(v) + 0.5; }              // crisp 1px lines

  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  // ---------------------------------------------------------------- the chart
  function CandleChart(container, options) {
    this.container = container;
    this.opts = Object.assign({
      height: 540, upColor: "#16a34a", downColor: "#dc2626", volumeShare: 0.24, minCandles: 10,
      maColors: {20: "#2563eb", 50: "#f59e0b", 200: "#7c3aed"}, onChange: null, interval: "daily"
    }, options || {});
    this.rows = [];
    this.range = "6M";
    this.interval = this.opts.interval;
    this.start = 0;
    this.end = 0;
    this.ma = {};
    this.maValues = {};
    this.hover = null;
    this._build();
  }

  CandleChart.prototype._build = function () {
    var self = this;
    this.container.classList.add("cc-root");
    this.legend = document.createElement("div");
    this.legend.className = "cc-legend";
    this.container.appendChild(this.legend);
    this.svg = el("svg", {"class": "cc-svg", role: "img", "aria-label": "candlestick chart"}, this.container);
    this.layerStatic = el("g", {}, this.svg);
    this.layerOverlay = el("g", {"class": "cc-overlay"}, this.svg);
    this.svg.addEventListener("pointermove", function (e) { self._onMove(e); });
    this.svg.addEventListener("pointerleave", function () { self.hover = null; self._drawOverlay(); });
    this.svg.addEventListener("pointerdown", function (e) { self._onDown(e); });
    this.svg.addEventListener("pointerup", function (e) { self._drag = null; self.svg.classList.remove("cc-dragging"); });
    this.svg.addEventListener("wheel", function (e) { self._onWheel(e); }, {passive: false});
    this.svg.addEventListener("dblclick", function () { self.setRange(self.range); });
  };

  CandleChart.prototype.setData = function (rows, range, interval) {
    this.rows = rows || [];
    if (interval) { this.interval = interval; }
    this.maValues = {};
    for (var p in this.ma) { if (this.ma[p]) { this.maValues[p] = sma(this.rows, +p); } }
    this.setRange(range || this.range);
  };

  CandleChart.prototype.setRange = function (range) {
    this.range = range;
    var w = windowForRange(this.rows, range);
    this.setWindow(w[0], w[1], true);
  };

  CandleChart.prototype.setWindow = function (start, end, fromRange) {
    var n = this.rows.length;
    var minCount = Math.min(this.opts.minCandles, n);
    var count = Math.max(minCount, Math.min(end - start, n));
    end = Math.min(Math.max(end, count), n);
    start = Math.max(0, end - count);
    this.start = start;
    this.end = end;
    this.customWindow = !fromRange;
    this.render();
    if (this.opts.onChange) { this.opts.onChange(summarize(this.rows, start, end), this); }
  };

  CandleChart.prototype.setMovingAverage = function (period, on) {
    this.ma[period] = !!on;
    if (on && !this.maValues[period]) { this.maValues[period] = sma(this.rows, period); }
    this.render();
  };

  CandleChart.prototype._geometry = function () {
    var width = Math.max(320, this.container.clientWidth || 800);
    var height = this.opts.height;
    var m = {top: 10, right: 74, bottom: 26, left: 8};
    var plotW = width - m.left - m.right, plotH = height - m.top - m.bottom, gap = 14;
    var volH = Math.round((plotH - gap) * this.opts.volumeShare);
    var priceH = plotH - gap - volH;
    var count = Math.max(1, this.end - this.start);
    var lo = Infinity, hi = -Infinity, vmax = 0;
    for (var i = this.start; i < this.end; i++) {
      var r = this.rows[i];
      if (r[L] < lo) { lo = r[L]; }
      if (r[H] > hi) { hi = r[H]; }
      if (r[V] > vmax) { vmax = r[V]; }
      for (var p in this.ma) {
        var v = this.ma[p] && this.maValues[p] ? this.maValues[p][i] : null;
        if (v !== null && v !== undefined) { if (v < lo) { lo = v; } if (v > hi) { hi = v; } }
      }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    var pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.02 || 1;
    return {
      width: width, height: height, m: m, plotW: plotW, priceTop: m.top, priceH: priceH,
      volTop: m.top + priceH + gap, volH: volH, slot: plotW / count, count: count,
      lo: lo - pad, hi: hi + pad, vmax: vmax || 1
    };
  };

  CandleChart.prototype._x = function (g, i) { return g.m.left + (i - this.start + 0.5) * g.slot; };
  CandleChart.prototype._y = function (g, price) { return g.priceTop + (g.hi - price) / (g.hi - g.lo) * g.priceH; };
  CandleChart.prototype._yv = function (g, volume) { return g.volTop + g.volH - volume / g.vmax * g.volH; };

  CandleChart.prototype.render = function () {
    var g = this._geometry();
    this.geo = g;
    this.svg.setAttribute("width", g.width);
    this.svg.setAttribute("height", g.height);
    this.svg.setAttribute("viewBox", "0 0 " + g.width + " " + g.height);
    var layer = this.layerStatic;
    while (layer.firstChild) { layer.removeChild(layer.firstChild); }
    if (!this.rows.length) {
      text(layer, g.width / 2, g.height / 2, "no data", {"class": "cc-empty", "text-anchor": "middle"});
      this._drawOverlay();
      return;
    }
    var right = g.m.left + g.plotW;
    var last = this.rows[this.end - 1];
    var yl = px(this._y(g, last[C]));
    // grid + price axis (a tick label too close to the last-close tag is left out)
    var ticks = niceTicks(g.lo, g.hi, Math.max(3, Math.round(g.priceH / 60))).ticks;
    var decimals = decimalsFor(g.hi);
    var grid = el("g", {"class": "cc-grid"}, layer);
    ticks.forEach(function (t) {
      var y = px(this._y(g, t));
      el("line", {x1: g.m.left, x2: right, y1: y, y2: y}, grid);
      if (Math.abs(y - yl) >= 14) {
        text(layer, right + 6, y + 4, formatPrice(t, decimals), {"class": "cc-axis"});
      }
    }, this);
    // volume axis
    var vt = niceTicks(0, g.vmax, 2).ticks.filter(function (t) { return t > 0 && t <= g.vmax; });
    vt.forEach(function (t) {
      var y = px(this._yv(g, t));
      el("line", {x1: g.m.left, x2: right, y1: y, y2: y}, grid);
      text(layer, right + 6, y + 4, formatVolume(t), {"class": "cc-axis"});
    }, this);
    el("line", {x1: g.m.left, x2: right, y1: px(g.volTop + g.volH), y2: px(g.volTop + g.volH), "class": "cc-baseline"}, layer);
    text(layer, g.m.left + 4, g.volTop + 11, "Volume", {"class": "cc-pane-label"});
    // time axis
    timeLabels(this.rows, this.start, this.end, g.slot, 70, this.interval).forEach(function (lab) {
      var x = px(this._x(g, lab.index));
      el("line", {x1: x, x2: x, y1: g.priceTop, y2: g.volTop + g.volH, "class": lab.major ? "cc-vgrid cc-major" : "cc-vgrid"}, grid);
      // labels near the edges are anchored inwards so they are not cut off
      var anchor = x - g.m.left < 30 ? "start" : right - x < 30 ? "end" : "middle";
      text(layer, anchor === "start" ? Math.max(x - 4, g.m.left) : x, g.height - 8, lab.text,
           {"class": "cc-axis", "text-anchor": anchor});
    }, this);
    // candles, wicks and volume bars: one path per colour (fast for thousands of days).
    // With more days than pixels, neighbouring days are merged into one candle per ~2 pixels
    // (open of the first day, highest high, lowest low, close of the last day; the volume bar
    // shows their average daily volume) - the legend still shows the hovered day itself.
    var group = g.slot < 2 ? Math.ceil(2 / g.slot) : 1;
    this.groupSize = group;
    var bodyW = Math.max(1, Math.min(g.slot * group * 0.72, 22));
    var paths = {up: {wick: "", body: "", vol: ""}, down: {wick: "", body: "", vol: ""}};
    var volBase = g.volTop + g.volH;
    for (var i = this.start; i < this.end; i += group) {
      var stop = Math.min(i + group, this.end);
      var r = mergeRows(this.rows, i, stop);
      var side = r[C] >= r[O] ? paths.up : paths.down;
      var x = g.m.left + ((i - this.start) + (stop - i) / 2) * g.slot, xl = x - bodyW / 2;
      var yo = this._y(g, r[O]), yc = this._y(g, r[C]);
      var top = Math.min(yo, yc), h = Math.max(1, Math.abs(yc - yo));
      side.wick += "M" + px(x) + " " + this._y(g, r[H]).toFixed(1) + "V" + this._y(g, r[L]).toFixed(1);
      side.body += "M" + xl.toFixed(1) + " " + top.toFixed(1) + "h" + bodyW.toFixed(1) + "v" + h.toFixed(1) +
                   "h" + (-bodyW).toFixed(1) + "Z";
      var yv = this._yv(g, r[V] / (stop - i));
      side.vol += "M" + xl.toFixed(1) + " " + yv.toFixed(1) + "h" + bodyW.toFixed(1) + "V" + volBase.toFixed(1) +
                  "h" + (-bodyW).toFixed(1) + "Z";
    }
    var colors = {up: this.opts.upColor, down: this.opts.downColor};
    ["up", "down"].forEach(function (k) {
      el("path", {d: paths[k].vol, fill: colors[k], "fill-opacity": 0.45, "class": "cc-volume cc-" + k}, layer);
      el("path", {d: paths[k].wick, stroke: colors[k], "stroke-width": 1, fill: "none", "class": "cc-wick cc-" + k}, layer);
      el("path", {d: paths[k].body, fill: colors[k], stroke: colors[k], "stroke-width": bodyW > 3 ? 0.5 : 0,
                  "class": "cc-body cc-" + k}, layer);
    });
    // moving averages
    for (var p in this.ma) {
      if (!this.ma[p] || !this.maValues[p]) { continue; }
      var d = "", pen = "M";
      for (var j = this.start; j < this.end; j += group) {
        var v = this.maValues[p][j];
        if (v === null) { pen = "M"; continue; }
        d += pen + this._x(g, j).toFixed(1) + " " + this._y(g, v).toFixed(1);
        pen = "L";
      }
      el("path", {d: d, fill: "none", stroke: this.opts.maColors[p] || "#64748b", "stroke-width": 1.4,
                  "class": "cc-ma cc-ma-" + p}, layer);
    }
    // last close of the window
    var lastColor = last[C] >= last[O] ? this.opts.upColor : this.opts.downColor;
    el("line", {x1: g.m.left, x2: right, y1: yl, y2: yl, stroke: lastColor, "class": "cc-lastline"}, layer);
    el("rect", {x: right + 1, y: yl - 9, width: g.m.right - 3, height: 18, rx: 3, fill: lastColor}, layer);
    text(layer, right + 6, yl + 4, formatPrice(last[C], decimals), {"class": "cc-lastlabel"});
    var unit = UNITS[this.interval] || UNITS.daily, count = this.end - this.start;
    this.svg.setAttribute("aria-label", this.interval + " candlestick chart, " + count + " " +
                          unit[count === 1 ? 0 : 1] + " from " + this.rows[this.start][D] + " to " + last[D]);
    this._drawOverlay();
  };

  CandleChart.prototype._indexAt = function (clientX) {
    var g = this.geo, rect = this.svg.getBoundingClientRect();
    var i = this.start + Math.floor((clientX - rect.left - g.m.left) / g.slot);
    return Math.max(this.start, Math.min(this.end - 1, i));
  };

  CandleChart.prototype._onMove = function (e) {
    if (!this.rows.length) { return; }
    if (this._drag) {
      var shift = Math.round((this._drag.x - e.clientX) / this.geo.slot);
      if (shift !== 0) {
        this.setWindow(this._drag.start + shift, this._drag.end + shift, false);
      }
      return;
    }
    var rect = this.svg.getBoundingClientRect();
    this.hover = {index: this._indexAt(e.clientX), y: e.clientY - rect.top};
    this._drawOverlay();
  };

  CandleChart.prototype._onDown = function (e) {
    if (!this.rows.length || e.button !== 0) { return; }
    this._drag = {x: e.clientX, start: this.start, end: this.end};
    this.svg.classList.add("cc-dragging");
    if (this.svg.setPointerCapture) { try { this.svg.setPointerCapture(e.pointerId); } catch (ignore) { /* jsdom */ } }
  };

  CandleChart.prototype._onWheel = function (e) {
    if (!this.rows.length) { return; }
    e.preventDefault();
    var anchor = this._indexAt(e.clientX);
    var count = this.end - this.start;
    var factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    var newCount = Math.round(Math.max(this.opts.minCandles, Math.min(this.rows.length, count * factor)));
    if (newCount === count) { newCount = count + (e.deltaY > 0 ? 1 : -1); }
    var ratio = (anchor - this.start) / Math.max(1, count);
    var start = Math.round(anchor - ratio * newCount);
    this.setWindow(start, start + newCount, false);
  };

  CandleChart.prototype._drawOverlay = function () {
    var layer = this.layerOverlay, g = this.geo;
    while (layer.firstChild) { layer.removeChild(layer.firstChild); }
    if (!g || !this.rows.length) { this.legend.innerHTML = ""; return; }
    var index = this.hover ? this.hover.index : this.end - 1;
    var r = this.rows[index];
    if (this.hover) {
      var x = px(this._x(g, index));
      el("line", {x1: x, x2: x, y1: g.priceTop, y2: g.volTop + g.volH, "class": "cc-cross"}, layer);
      var y = this.hover.y;
      if (y >= g.priceTop && y <= g.priceTop + g.priceH) {
        el("line", {x1: g.m.left, x2: g.m.left + g.plotW, y1: px(y), y2: px(y), "class": "cc-cross"}, layer);
        var price = g.hi - (y - g.priceTop) / g.priceH * (g.hi - g.lo);
        el("rect", {x: g.m.left + g.plotW + 1, y: y - 9, width: g.m.right - 3, height: 18, rx: 3, "class": "cc-crosslabel-bg"}, layer);
        text(layer, g.m.left + g.plotW + 6, y + 4, formatPrice(price, decimalsFor(g.hi)), {"class": "cc-crosslabel"});
      }
      var tag = periodLabel(r, this.interval, false), tw = Math.max(86, tag.length * 7 + 14);
      el("rect", {x: x - tw / 2, y: g.height - g.m.bottom + 3, width: tw, height: 18, rx: 3, "class": "cc-crosslabel-bg"}, layer);
      text(layer, x, g.height - g.m.bottom + 16, tag, {"class": "cc-crosslabel", "text-anchor": "middle"});
    }
    var prev = index > 0 ? this.rows[index - 1][C] : null;
    var change = prev ? (r[C] - prev) / prev * 100 : null;
    var cls = r[C] >= r[O] ? "cc-up" : "cc-down";
    var parts = [
      "<b>" + esc(periodLabel(r, this.interval, true)) + "</b>",
      "O <span class='" + cls + "'>" + formatPrice(r[O]) + "</span>",
      "H <span class='" + cls + "'>" + formatPrice(r[H]) + "</span>",
      "L <span class='" + cls + "'>" + formatPrice(r[L]) + "</span>",
      "C <span class='" + cls + "'>" + formatPrice(r[C]) + "</span>",
      "<span class='" + (change === null ? "" : change >= 0 ? "cc-up" : "cc-down") + "'>" + formatPercent(change) + "</span>",
      "Vol <span class='" + cls + "'>" + formatVolume(r[V]) + "</span>"
    ];
    if (this.interval !== "daily" && r.length > DAYS) {
      parts.push("<span class='cc-days'>" + r[DAYS] + " trading day" + (r[DAYS] === 1 ? "" : "s") + "</span>");
      if (r[COMPLETE] === false) {
        parts.push("<span class='cc-incomplete' title='a trading day of this period is missing in the source data'>" +
                   "incomplete</span>");
      }
    }
    for (var p in this.ma) {
      if (this.ma[p] && this.maValues[p]) {
        parts.push("<span style='color:" + (this.opts.maColors[p] || "#64748b") + "'>MA" + p + " " +
                   formatPrice(this.maValues[p][index]) + "</span>");
      }
    }
    this.legend.innerHTML = parts.join(" &nbsp; ");
  };

  return {
    CandleChart: CandleChart, niceTicks: niceTicks, formatPrice: formatPrice, formatVolume: formatVolume,
    formatPercent: formatPercent, sma: sma, windowForRange: windowForRange, summarize: summarize,
    timeLabels: timeLabels, addMonths: addMonths, mergeRows: mergeRows, isoWeek: isoWeek, periodLabel: periodLabel
  };
});
