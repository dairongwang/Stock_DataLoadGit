/*
 * The chart page: ticker search, the watchlist (add / remove stocks, step up and down through
 * it, go to one from the drop-down), daily / weekly / monthly candles, range buttons, moving
 * averages, the summary of the visible period, CSV download.  Talks to the JSON API of this
 * app only (/api/daily|weekly|monthly/<ticker>, /api/quotes, /api/tickers).
 */
(function () {
  "use strict";
  var body = document.body;
  var api = body.getAttribute("data-api") || "";
  var state = {ticker: body.getAttribute("data-ticker") || "", range: body.getAttribute("data-range") || "6M",
               interval: body.getAttribute("data-interval") || "daily", data: null, request: 0};
  var WORDS = {
    daily: {unit: "trading days", one: "trading day", volume: "Avg. daily volume", count: "Trading days"},
    weekly: {unit: "weeks", one: "week", volume: "Avg. weekly volume", count: "Weeks"},
    monthly: {unit: "months", one: "month", volume: "Avg. monthly volume", count: "Months"}
  };

  var $ = function (id) { return document.getElementById(id); };
  var F = window.Candlestick;
  var W = window.Watchlist;

  var chart = new F.CandleChart($("chart"), {
    height: Math.max(380, Math.min(640, window.innerHeight - 260)),
    interval: state.interval,
    onChange: function (summary, c) {
      showSummary(summary);
      markRange(c.customWindow ? null : c.range);
    }
  });

  function setStatus(message, kind) {
    var box = $("status");
    box.textContent = message || "";
    box.className = "status" + (message ? " " + (kind || "info") : "");
  }

  function markRange(range) {
    document.querySelectorAll("[data-range-button]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-range-button") === range);
    });
  }

  function markInterval(interval) {
    document.querySelectorAll("[data-interval-button]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-interval-button") === interval);
    });
    var w = WORDS[interval] || WORDS.daily;
    $("l-volume").textContent = w.volume;
    $("l-days").textContent = w.count;
  }

  function showSummary(s) {
    var set = function (id, value, cls) {
      var node = $(id);
      node.textContent = value;
      node.className = "value" + (cls ? " " + cls : "");
    };
    if (!s) {
      ["s-close", "s-change", "s-high", "s-low", "s-volume", "s-days"].forEach(function (id) { set(id, "-"); });
      $("s-period").textContent = "";
      return;
    }
    var dir = s.change >= 0 ? "up" : "down";
    set("s-close", F.formatPrice(s.close));
    set("s-change", (s.change >= 0 ? "+" : "") + F.formatPrice(s.change) + "  (" + F.formatPercent(s.changePct) + ")", dir);
    set("s-high", F.formatPrice(s.high));
    set("s-low", F.formatPrice(s.low));
    set("s-volume", F.formatVolume(s.avgVolume));
    set("s-days", String(s.days));
    $("s-period").textContent = s.first + " .. " + s.last;
  }

  function updateUrl() {
    var url = new URL(window.location.href);
    url.searchParams.set("ticker", state.ticker);
    url.searchParams.set("range", state.range);
    url.searchParams.set("interval", state.interval);
    if (typeof L !== "undefined" && L.activeId) { url.searchParams.set("list", String(L.activeId)); }
    if (!state.ticker) { url.searchParams.delete("ticker"); }
    window.history.replaceState(null, "", url.pathname + "?" + url.searchParams.toString());
  }

  function loadTicker(ticker) {
    ticker = (ticker || "").trim().toUpperCase();
    if (!ticker) { return; }
    var request = ++state.request, interval = state.interval;
    state.requested = ticker;                  // the watchlist position follows the request, even if it fails
    renderWatchlist();
    setStatus("Loading " + ticker + " (" + interval + ") …");
    fetch(api + "/api/" + interval + "/" + encodeURIComponent(ticker), {headers: {"Accept": "application/json"}})
      .then(function (resp) {
        return resp.json().then(function (json) { return {ok: resp.ok, body: json}; });
      })
      .then(function (res) {
        if (request !== state.request) { return; }                 // a newer request won
        if (!res.ok) {
          setStatus(res.body.error || "the data could not be loaded", "error");
          return;
        }
        var d = res.body, w = WORDS[interval] || WORDS.daily;
        state.ticker = d.ticker;
        state.data = d;
        $("ticker").value = d.ticker;
        $("title-ticker").textContent = d.ticker;
        $("title-meta").textContent = (d.market ? d.market + " · " : "") + d.count.toLocaleString("en-US") +
          " " + (d.count === 1 ? w.one : w.unit) + " · " + d.first + " .. " + d.last + (d.truncated ? " (the most recent " +
          d.count.toLocaleString("en-US") + " of " + d.total.toLocaleString("en-US") + ")" : "");
        $("source").textContent = "source: " + (d.source || interval);
        $("csv").href = api + "/api/" + interval + "/" + encodeURIComponent(d.ticker) + "?format=csv";
        $("csv").classList.remove("hidden");
        document.title = d.ticker + (interval === "daily" ? "" : " (" + interval + ")") + " - " + body.getAttribute("data-title");
        chart.setData(d.rows, state.range, interval);
        updateUrl();
        setStatus("");
        renderWatchlist();
      })
      .catch(function (err) {
        if (request === state.request) { setStatus("the data could not be loaded: " + err, "error"); }
      });
  }

  // -- the stock lists (saved in the database: /api/lists) --------------------------------
  //    the watchlist, the Selected list and the screen lists loaded from text files
  var storage = null;
  try { storage = window.localStorage; } catch (ignore) { storage = null; }
  var ACTIVE_KEY = "stock_chart.active_list";           // a view preference only - the lists are in the database
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var defaults = W.parse(body.getAttribute("data-watchlist") || "");
  var L = {lists: [], activeId: null, items: [], quotes: {}, unknown: {}, watch: null, selected: null,
           max: W.MAX, ready: false, failed: false};

  function current() { return state.requested || state.ticker; }
  function active() { return L.lists.filter(function (l) { return l.id === L.activeId; })[0] || null; }
  function safeId(t) { return t.replace(/[^A-Za-z0-9_-]/g, "_"); }

  function wlMessage(text, kind) {
    var box = $("wl-msg");
    box.textContent = text || "";
    box.className = "wl-msg" + (text ? " " + (kind || "ok") : "");
  }

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined) { n.textContent = text; }
    return n;
  }

  /** fetch + JSON; writes carry the X-Stock-Chart header the server demands (CSRF protection). */
  function call(method, path, payload) {
    var opts = {method: method, headers: {"Accept": "application/json"}};
    if (method !== "GET") { opts.headers["X-Stock-Chart"] = "1"; }
    if (payload instanceof FormData) {
      opts.body = payload;
    } else if (payload !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(payload);
    }
    return fetch(api + path, opts).then(function (r) {
      return r.json().catch(function () { return {error: "HTTP " + r.status}; })
        .then(function (b) { return {ok: r.ok, status: r.status, body: b}; });
    });
  }

  function listLabel(l) {
    return (l.kind === "screen" ? "Screen: " : "") + l.name + " (" + l.count + ")";
  }

  /** Take over a list as the server returned it (summary + tickers). */
  function absorb(list) {
    var found = false;
    L.lists = L.lists.map(function (l) {
      if (l.id !== list.id) { return l; }
      found = true;
      return Object.assign({}, l, list);
    });
    if (!found) { L.lists.push(list); }
    if (list.kind === "watch") { L.watch = Object.assign({}, L.watch || {}, list); }
    if (list.kind === "selected") { L.selected = Object.assign({}, L.selected || {}, list); }
    if (list.id === L.activeId && list.tickers) { L.items = list.tickers.slice(); }
  }

  function renderLists() {
    var select = $("list-select");
    while (select.firstChild) { select.removeChild(select.firstChild); }
    if (L.failed) {
      select.appendChild(node("option", "", "lists unavailable"));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    var screens = L.lists.filter(function (l) { return l.kind === "screen"; });
    L.lists.filter(function (l) { return l.kind !== "screen"; }).forEach(function (l) {
      var o = node("option", "", listLabel(l));
      o.value = String(l.id);
      select.appendChild(o);
    });
    if (screens.length) {
      var group = document.createElement("optgroup");
      group.label = "Screen lists";
      screens.forEach(function (l) {
        var o = node("option", "", l.name + " (" + l.count + ")");
        o.value = String(l.id);
        group.appendChild(o);
      });
      select.appendChild(group);
    }
    select.value = String(L.activeId);
    var a = active();
    $("list-name").textContent = a ? (a.kind === "screen" ? "Screen list: " : "") + a.name : "";
    $("wl-reset").classList.toggle("hidden", !(a && a.kind === "watch"));
    $("list-delete").classList.toggle("hidden", !(a && a.deletable));
    $("wl-add").placeholder = "add ticker(s) to " + (a ? a.name : "the list") + ", e.g. PFE";
  }

  function renderWatchlist() {
    renderLists();
    var list = L.items, cur = current(), pos = W.position(list, cur);
    $("wl-count").textContent = "(" + list.length + ")";
    // the list
    var ul = $("wl-list");
    while (ul.firstChild) { ul.removeChild(ul.firstChild); }
    if (!list.length) {
      ul.appendChild(node("li", "empty", L.failed ? "The lists cannot be read - see the message below." :
        (L.ready ? "The list is empty - add a ticker below." : "loading …")));
    }
    list.forEach(function (t) {
      var q = L.quotes[t], li = node("li", t === cur ? "active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("data-ticker", t);
      li.setAttribute("aria-selected", t === cur ? "true" : "false");
      li.id = "wl-item-" + safeId(t);
      li.appendChild(node("span", "wl-ticker", t));
      li.appendChild(node("span", "wl-price", q ? F.formatPrice(q.close) : ""));
      var remove = node("button", "wl-remove", "\u00d7");
      remove.type = "button";
      remove.title = "remove " + t + " from the list";
      remove.setAttribute("aria-label", "remove " + t + " from the list");
      remove.setAttribute("data-remove", t);
      li.appendChild(remove);
      var when = q ? MONTHS[+q.date.slice(5, 7) - 1] + " " + (+q.date.slice(8, 10)) : "";
      var market = node("span", "wl-market", q ? (q.market ? q.market + " · " : "") + when : "");
      if (q) { market.title = "last trading day " + q.date; }
      li.appendChild(market);
      var change = node("span", "wl-change");
      if (q && q.change !== null) {
        // the change with the precision of the price itself (2 decimals from 1.00 upwards)
        var decimals = Math.abs(q.close) >= 1 ? 2 : 4;
        change.textContent = (q.change >= 0 ? "+" : "") + F.formatPrice(q.change, decimals) + " (" +
                             F.formatPercent(q.change_pct) + ")";
        change.className += q.change >= 0 ? " up" : " down";
      } else if (L.unknown[t]) {
        change.textContent = "no data";
        change.className += " nodata";
      }
      li.appendChild(change);
      ul.appendChild(li);
    });
    if (pos.index >= 0) {
      ul.setAttribute("aria-activedescendant", "wl-item-" + safeId(cur));
      var activeLi = ul.querySelector("li.active");
      if (activeLi && activeLi.scrollIntoView) { activeLi.scrollIntoView({block: "nearest"}); }
    } else {
      ul.removeAttribute("aria-activedescendant");
    }
    // go to: the drop-down and the symbols for typing
    var select = $("wl-select"), goto = $("wl-goto-list");
    while (select.firstChild) { select.removeChild(select.firstChild); }
    while (goto.firstChild) { goto.removeChild(goto.firstChild); }
    if (pos.index < 0) {
      var placeholder = node("option", "", list.length ? (cur ? cur + " - not in the list" : "choose a stock")
                                                        : "the list is empty");
      placeholder.value = "";
      placeholder.disabled = true;
      placeholder.selected = true;
      select.appendChild(placeholder);
    }
    list.forEach(function (t, i) {
      var q = L.quotes[t];
      var o = node("option", "", (i + 1) + ". " + t + (q && q.market ? " · " + q.market : ""));
      o.value = t;
      o.selected = t === cur;
      select.appendChild(o);
      var g = node("option");
      g.value = t;
      goto.appendChild(g);
    });
    select.disabled = !list.length;
    // previous / next, position
    $("wl-prev").disabled = W.neighbour(list, cur, -1) === null || pos.index === 0;
    $("wl-next").disabled = W.neighbour(list, cur, 1) === null;
    $("wl-pos").textContent = list.length ? (pos.index >= 0 ? (pos.index + 1) : "–") + " / " + list.length : "";
    renderToggles();
  }

  /** The buttons next to the title: the stock on show in the Selected list / the watchlist. */
  function renderToggles() {
    var shown = state.ticker;
    [["sel-toggle", L.selected, "\u2713 Selected - remove", "+ Add to Selected"],
     ["wl-toggle", L.watch, "\u2605 In watchlist - remove", "\u2606 Add to watchlist"]].forEach(function (spec) {
      var button = $(spec[0]), list = spec[1];
      button.classList.toggle("hidden", !shown || !list || L.failed);
      if (!shown || !list) { return; }
      var inList = (list.tickers || []).indexOf(shown) >= 0;
      button.textContent = inList ? spec[2] : spec[3];
      button.classList.toggle("in", inList);
      button.classList.toggle("sel", spec[0] === "sel-toggle");
      button.setAttribute("data-in", inList ? "1" : "");
      button.setAttribute("aria-pressed", inList ? "true" : "false");
    });
  }

  function refreshQuotes() {
    var wanted = L.items.filter(function (t) { return !L.quotes[t] && !L.unknown[t]; });
    if (!wanted.length) { renderWatchlist(); return Promise.resolve(); }
    var chunks = [];
    for (var i = 0; i < wanted.length; i += 500) { chunks.push(wanted.slice(i, i + 500)); }
    return Promise.all(chunks.map(function (chunk) {
      return fetch(api + "/api/quotes?tickers=" + encodeURIComponent(chunk.join(",")))
        .then(function (r) { return r.ok ? r.json() : {quotes: [], unknown: []}; })
        .then(function (d) {
          d.quotes.forEach(function (q) { L.quotes[q.ticker] = q; });
          d.unknown.forEach(function (t) { L.unknown[t] = true; });
        });
    })).catch(function () { /* the figures are optional */ }).then(renderWatchlist);
  }

  /** Make *id* the list on show; *show*: open its first stock unless the stock on show is in it. */
  function activate(id, show) {
    var l = L.lists.filter(function (x) { return x.id === id; })[0] || L.watch;
    if (!l) { return Promise.resolve(); }
    L.activeId = l.id;
    try { if (storage) { storage.setItem(ACTIVE_KEY, String(l.id)); } } catch (ignore) { /* preference only */ }
    var ready = call("GET", "/api/lists/" + l.id).then(function (res) {      // always fresh: lists can be shared
      if (!res.ok) { throw new Error(res.body.error || "the list could not be read"); }
      return res.body;
    });
    return ready.then(function (full) {
      absorb(full);
      L.items = (full.tickers || []).slice();
      updateUrl();
      renderWatchlist();
      if (show && L.items.length && L.items.indexOf(current()) < 0) { loadTicker(L.items[0]); }
      return refreshQuotes();
    }).catch(function (err) { wlMessage(String(err.message || err), "error"); });
  }

  function go(step) {
    var target = W.neighbour(L.items, current(), step);
    if (target) { loadTicker(target); }
  }

  /** Tell what an add / replace did: added, already there, without data, invalid. */
  function describe(body, verb) {
    var parts = [];
    if (body.added && body.added.length) { parts.push(verb + " " + body.added.join(", ")); }
    if (body.already && body.already.length) {
      parts.push(body.already.join(", ") + (body.already.length === 1 ? " is" : " are") + " already in the list");
    }
    if (body.unknown && body.unknown.length) { parts.push("no data for " + body.unknown.join(", ")); }
    if (body.invalid && body.invalid.length) { parts.push("not a ticker: " + body.invalid.join(", ")); }
    return parts.join("; ");
  }

  /** Add the tickers of *text* to the list *listId*; *show* opens the first one added. */
  function addTo(listId, text, show) {
    var wanted = W.parse(text);
    if (!wanted.length) {
      wlMessage(String(text).trim() ? "'" + String(text).trim() + "' is not a valid ticker" : "type a ticker first", "error");
      return Promise.resolve([]);
    }
    return call("POST", "/api/lists/" + listId + "/items", {tickers: wanted}).then(function (res) {
      if (!res.ok) { wlMessage(res.body.error || "the list could not be changed", "error"); return []; }
      absorb(res.body.list);
      var problems = (res.body.already || []).length + (res.body.unknown || []).length + (res.body.invalid || []).length;
      wlMessage(describe(res.body, "added") + (res.body.added.length ? " to " + res.body.list.name : ""),
                problems ? "error" : "ok");
      renderWatchlist();
      refreshQuotes();
      if (show && res.body.added.length) { loadTicker(res.body.added[0]); }
      return res.body.added;
    }).catch(function (err) { wlMessage("the list could not be changed: " + err, "error"); return []; });
  }

  function removeFrom(listId, ticker) {
    return call("DELETE", "/api/lists/" + listId + "/items/" + encodeURIComponent(ticker)).then(function (res) {
      if (!res.ok) { wlMessage(res.body.error || "the list could not be changed", "error"); return; }
      absorb(res.body.list);
      wlMessage("removed " + ticker + " from " + res.body.list.name, "ok");
      renderWatchlist();
    }).catch(function (err) { wlMessage("the list could not be changed: " + err, "error"); });
  }

  function toggle(list) {
    var t = state.ticker;
    if (!t || !list) { return; }
    if ((list.tickers || []).indexOf(t) >= 0) { removeFrom(list.id, t); } else { addTo(list.id, t, false); }
  }

  function loadScreenFile(file) {
    if (!file) { return; }
    var form = new FormData();
    form.append("file", file, file.name);
    wlMessage("loading " + file.name + " …", "ok");
    call("POST", "/api/lists/screen", form).then(function (res) {
      if (!res.ok) { wlMessage(res.body.error || "the file could not be loaded", "error"); return; }
      var b = res.body, notes = [];
      if (b.without_data.length) { notes.push(b.without_data.length + " without data: " + b.without_data.join(", ")); }
      if (b.invalid.length) { notes.push("not a ticker: " + b.invalid.join(", ")); }
      absorb(b.list);
      wlMessage((b.replaced ? "reloaded" : "loaded") + " screen list " + b.list.name + ": " + b.symbols + " symbol" +
                (b.symbols === 1 ? "" : "s") + (notes.length ? " (" + notes.join("; ") + ")" : ""),
                notes.length ? "error" : "ok");
      L.quotes = {};
      L.unknown = {};
      L.activeId = null;
      state.requested = null;
      activate(b.list.id, false).then(function () { if (L.items.length) { loadTicker(L.items[0]); } });
    }).catch(function (err) { wlMessage("the file could not be loaded: " + err, "error"); });
  }

  /** Once: a watchlist the browser kept (earlier versions) moves into the database. */
  function migrateBrowserList() {
    var local = null;
    try { local = storage ? storage.getItem(W.KEY) : null; } catch (ignore) { local = null; }
    if (local === null || !L.watch) { return Promise.resolve(); }
    var tickers = W.load(storage, []);
    var done = function () { try { storage.removeItem(W.KEY); } catch (ignore) { /* gone anyway */ } };
    if (L.watch.edited || !tickers.length) { done(); return Promise.resolve(); }
    return call("PUT", "/api/lists/" + L.watch.id + "/items", {tickers: tickers}).then(function (res) {
      if (res.ok) {
        absorb(res.body.list);
        done();
        wlMessage("the watchlist of this browser was moved into the database", "ok");
      }
    }).catch(function () { /* try again next time */ });
  }

  function loadLists() {
    return call("GET", "/api/lists").then(function (res) {
      if (!res.ok) { throw new Error(res.body.error || "the lists could not be read"); }
      L.lists = res.body.lists;
      L.max = res.body.max_items || L.max;
      L.watch = L.lists.filter(function (l) { return l.kind === "watch"; })[0] || null;
      L.selected = L.lists.filter(function (l) { return l.kind === "selected"; })[0] || null;
      L.ready = true;
      L.failed = false;
      return migrateBrowserList();
    });
  }

  $("list-select").addEventListener("change", function () { activate(+this.value, true); });
  $("wl-list").addEventListener("click", function (e) {
    var target = e.target;
    if (target.getAttribute && target.getAttribute("data-remove")) {
      removeFrom(L.activeId, target.getAttribute("data-remove"));
      return;
    }
    var li = target.closest ? target.closest("li[data-ticker]") : null;
    if (li) { loadTicker(li.getAttribute("data-ticker")); }
  });
  $("wl-list").addEventListener("keydown", function (e) {
    var list = L.items;
    if (!list.length) { return; }
    var target = null;
    if (e.key === "ArrowDown") { target = W.neighbour(list, current(), 1); }
    else if (e.key === "ArrowUp") { target = W.neighbour(list, current(), -1); }
    else if (e.key === "Home") { target = list[0]; }
    else if (e.key === "End") { target = list[list.length - 1]; }
    else if (e.key === "Delete" && W.position(list, current()).index >= 0) { removeFrom(L.activeId, current()); }
    else { return; }
    e.preventDefault();
    if (target && target !== current()) { loadTicker(target); }
  });
  $("wl-select").addEventListener("change", function () { if (this.value) { loadTicker(this.value); } });
  function gotoTyped() {
    var input = $("wl-goto"), t = W.normalize(input.value);
    if (!t) {
      wlMessage(input.value.trim() ? "'" + input.value.trim() + "' is not a valid ticker" : "type a symbol first", "error");
      return;
    }
    if (L.items.indexOf(t) < 0) { wlMessage(t + " is not in this list - showing it anyway", "error"); } else { wlMessage(""); }
    input.value = "";
    loadTicker(t);
  }
  $("wl-goto-form").addEventListener("submit", function (e) { e.preventDefault(); gotoTyped(); });
  $("wl-goto").addEventListener("input", function (e) {
    // picking a symbol from the suggestions goes there right away
    if (e.inputType === "insertReplacementText" || (e.inputType === undefined && L.items.indexOf(W.normalize(this.value)) >= 0)) {
      gotoTyped();
    }
  });
  $("wl-prev").addEventListener("click", function () { go(-1); });
  $("wl-next").addEventListener("click", function () { go(1); });
  $("wl-add-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = $("wl-add");
    if (!L.activeId) { return; }
    addTo(L.activeId, input.value, true).then(function (added) { if (added.length) { input.value = ""; } });
  });
  $("wl-reset").addEventListener("click", function () {
    if (!L.watch || !window.confirm("Replace the watchlist with the default list (" + (defaults.join(", ") || "empty") + ")?")) {
      return;
    }
    call("POST", "/api/lists/" + L.watch.id + "/reset").then(function (res) {
      if (!res.ok) { wlMessage(res.body.error || "the watchlist could not be reset", "error"); return; }
      absorb(res.body.list);
      wlMessage("the default list is back", "ok");
      refreshQuotes();
    });
  });
  $("list-delete").addEventListener("click", function () {
    var a = active();
    if (!a || !a.deletable || !window.confirm("Delete the screen list " + a.name + " (" + a.count + " stocks)?")) { return; }
    call("DELETE", "/api/lists/" + a.id).then(function (res) {
      if (!res.ok) { wlMessage(res.body.error || "the list could not be deleted", "error"); return; }
      L.lists = L.lists.filter(function (l) { return l.id !== a.id; });
      wlMessage("deleted the screen list " + a.name, "ok");
      activate(L.watch ? L.watch.id : null, false);
    });
  });
  $("screen-load").addEventListener("click", function () { $("screen-file").click(); });
  $("screen-file").addEventListener("change", function () {
    loadScreenFile(this.files && this.files[0]);
    this.value = "";                       // the same file can be loaded again
  });
  $("sel-toggle").addEventListener("click", function () { toggle(L.selected); });
  $("wl-toggle").addEventListener("click", function () { toggle(L.watch); });

  // -- ticker search (suggestions from the API) ------------------------------
  var suggestTimer = null;
  function suggest() {
    var q = this.value.trim().split(/[\s,;]+/).pop();
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(function () {
      fetch(api + "/api/tickers?limit=25&q=" + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : {tickers: []}; })
        .then(function (d) {
          var list = $("ticker-list");
          while (list.firstChild) { list.removeChild(list.firstChild); }
          d.tickers.forEach(function (t) {
            var o = document.createElement("option");
            o.value = t.ticker;
            o.label = t.ticker + (t.market ? " · " + t.market : "") + " · " + t.rows + " days (" + t.first + " .. " + t.last + ")";
            list.appendChild(o);
          });
        })
        .catch(function () { /* suggestions are optional */ });
    }, 200);
  }
  $("ticker").addEventListener("input", suggest);
  $("wl-add").addEventListener("input", suggest);
  $("ticker-form").addEventListener("submit", function (e) {
    e.preventDefault();
    loadTicker($("ticker").value);
  });

  // -- daily / weekly / monthly ---------------------------------------------------
  document.querySelectorAll("[data-interval-button]").forEach(function (b) {
    b.addEventListener("click", function () {
      var interval = b.getAttribute("data-interval-button");
      if (interval === state.interval) { return; }
      state.interval = interval;
      markInterval(interval);
      if (state.ticker) { loadTicker(state.ticker); } else { updateUrl(); }
    });
  });

  // -- range buttons / moving averages -----------------------------------------
  document.querySelectorAll("[data-range-button]").forEach(function (b) {
    b.addEventListener("click", function () {
      state.range = b.getAttribute("data-range-button");
      chart.setRange(state.range);
      updateUrl();
    });
  });
  document.querySelectorAll("[data-ma]").forEach(function (box) {
    box.addEventListener("change", function () { chart.setMovingAverage(+box.getAttribute("data-ma"), box.checked); });
    if (box.checked) { chart.setMovingAverage(+box.getAttribute("data-ma"), true); }
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { chart.render(); }, 120);
  });

  markRange(state.range);
  markInterval(state.interval);
  var fromUrl = !!body.getAttribute("data-ticker-from-url");
  function showEmpty() {
    showSummary(null);
    chart.setData([]);
    if (!$("status").textContent.trim()) {
      setStatus("The daily table has no rows yet. Fill it in the data pipeline, e.g. " +
                "'python -m stock_dataload promote daily' or 'promote eod --market NYSE' - this page shows it right away.");
    }
  }
  if (fromUrl && state.ticker) { loadTicker(state.ticker); }
  renderWatchlist();
  loadLists().then(function () {
    var wanted = +(new URL(window.location.href).searchParams.get("list") || 0);
    var stored = 0;
    try { stored = +(storage && storage.getItem(ACTIVE_KEY) || 0); } catch (ignore) { stored = 0; }
    var ids = L.lists.map(function (l) { return l.id; });
    var id = ids.indexOf(wanted) >= 0 ? wanted : ids.indexOf(stored) >= 0 ? stored : (L.watch ? L.watch.id : ids[0]);
    return activate(id, false).then(function () {
      // without ?ticker= in the address the page opens the first stock of the list
      if (!fromUrl) {
        if (L.items.length) { loadTicker(L.items[0]); } else if (state.ticker) { loadTicker(state.ticker); } else { showEmpty(); }
      }
    });
  }).catch(function (err) {
    L.failed = true;
    renderWatchlist();
    wlMessage(String(err.message || err), "error");
    if (!fromUrl) { if (state.ticker) { loadTicker(state.ticker); } else { showEmpty(); } }
  });
  window.stockChart = {chart: chart, load: loadTicker, state: state, lists: L};   // for debugging / browser tests
})();
