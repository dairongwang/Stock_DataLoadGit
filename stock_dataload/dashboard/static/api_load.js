/*
 * The API loading page: the three ways to start a load (selected stocks / whole market / text file),
 * the symbol types of the chosen market, the daily limits (the server enforces them too):
 *   - a whole market at most 3 times a day per market          -> #quota-block (error, blocks)
 *   - a text file: at most 5 loads a day and 100 symbols a file -> #file-block (error, blocks)
 *   - a stock at most 5 times a day                              -> #stock-notice (warning: those stocks are
 *                                                                   skipped; error + block when it is all of them)
 *   - one day of all stocks (bulk): a weekend / future date      -> #bulk-notice (error, blocks unless "call anyway")
 *     at most 3 calls for the same exchange and trading day      -> #bulk-call-block (error, blocks; the count is
 *                                                                   asked from /api/api-load/bulk-quota)
 *     (the bulk load of a whole exchange counts as a whole-market load -> #quota-block)
 * and the live progress of the running load (polls /api/api-load while a load is active).
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  // -- the start button: disabled while a load runs or when the daily limit of the market is reached ---
  var jobCard = $("job-card");
  var loadBusy = !!(jobCard && jobCard.getAttribute("data-active") === "1");
  var quotaBlocked = false, fileBlocked = false, stocksBlocked = false, bulkBlocked = false, callsBlocked = false;
  var refreshStart = function () {
    var start = $("start-button");
    if (!start) { return; }
    var limited = quotaBlocked || fileBlocked || stocksBlocked || bulkBlocked || callsBlocked;
    start.disabled = loadBusy || limited;
    start.title = limited ? "a daily limit is reached - see the notice above" :
                  loadBusy ? "an API load is running" : "";
    $("start-note").textContent = loadBusy ? "an API load is running - one at a time" :
                                  quotaBlocked ? "daily limit reached - choose selected stocks or another market" :
                                  limited ? "daily limit reached - see the notice above" : "";
  };

  // the symbols of a text typed in / read from a file - the same rules as the server (parse_symbol_text)
  var SYMBOL_RE = /^[A-Za-z0-9.\-_^=]{1,32}$/;
  var parseSymbols = function (text) {
    var symbols = [], invalid = [];
    String(text || "").replace(/^\ufeff/, "").split(/[,;\s]+/).forEach(function (token) {
      token = token.trim().replace(/^["']+|["']+$/g, "").trim();
      if (!token) { return; }
      if (!SYMBOL_RE.test(token)) {
        if (invalid.indexOf(token) < 0) { invalid.push(token.substr(0, 40)); }
        return;
      }
      token = token.toUpperCase();
      if (symbols.indexOf(token) < 0) { symbols.push(token); }
    });
    return {symbols: symbols, invalid: invalid};
  };
  var tickerOf = function (symbol) {          // MRK.US -> MRK: the daily limit counts per stock
    var i = symbol.lastIndexOf(".");
    return i > 0 && /^[A-Z0-9]+$/.test(symbol.substr(i + 1)) ? symbol.substr(0, i) : symbol;
  };

  // -- the form: which way, which market, which types ------------------------------------------
  var form = $("load-form");
  if (form) {
    var lists = [];
    try { lists = JSON.parse(form.getAttribute("data-lists") || "[]"); } catch (ignore) { lists = []; }
    var quotas = {};
    try { quotas = JSON.parse(form.getAttribute("data-quotas") || "{}"); } catch (ignore) { quotas = {}; }
    var dailyLimit = parseInt(form.getAttribute("data-limit"), 10) || 3;
    var fileQuota = {used: 0, limit: 5, started: []};
    try { fileQuota = JSON.parse(form.getAttribute("data-file-quota") || "{}"); } catch (ignore) { /* defaults */ }
    var maxFileSymbols = parseInt(form.getAttribute("data-max-file-symbols"), 10) || 100;
    var stockLimit = parseInt(form.getAttribute("data-stock-limit"), 10) || 5;
    var atLimit = {};
    try { atLimit = JSON.parse(form.getAttribute("data-stocks-at-limit") || "{}"); } catch (ignore) { atLimit = {}; }
    var fileSymbols = null, fileName = "";
    var bulkQuota = {};
    try { bulkQuota = JSON.parse(form.getAttribute("data-bulk-quota") || "{}"); } catch (ignore) { bulkQuota = {}; }
    var bulkQuotaUrl = form.getAttribute("data-bulk-quota-url");
    var bulkQuotaTimer = null;
    var typesBox = $("types-box");
    var defaults = (typesBox.getAttribute("data-default") || "").split("|").filter(Boolean);
    var chosen = null;                                  // the types ticked by the user (kept across markets)

    var currentMode = function () {
      return (form.querySelector("input[name=mode]:checked") || {}).value || "symbols";
    };

    var bulkMarket = function () { return ($("bulk_market").value || "US").trim().toUpperCase(); };
    var bulkSymbols = function () { return parseSymbols($("bulk_symbols").value).symbols; };

    var quotaOf = function (market) {
      return quotas[market] || {market: market, used: 0, limit: dailyLimit, remaining: dailyLimit,
                                exhausted: false, started: []};
    };

    var refusal = function (q) {
      var times = (q.started || []).map(function (t) { return String(t).substr(11, 5); })
                                   .filter(function (t) { return t; }).join(", ");
      return q.market + ": the bulk load of all stocks of a market may run at most " + q.limit +
             " times a day - it already ran " + q.used + " time(s) today" + (times ? " (at " + times + ")" : "") +
             ". Try again tomorrow, load selected stocks instead, or resume an unfinished run.";
    };

    // the daily limit of the chosen market: shown in the whole-market section, blocks the start button
    var showQuota = function () {
      var mode = currentMode();
      var market = mode === "bulk" ? bulkMarket() : currentMarket();
      var q = quotaOf(market);
      $("quota-count").textContent = (market || "?") + ": " + q.used + " of " + q.limit + " used today";
      var wholeMarket = mode === "market" || (mode === "bulk" && bulkSymbols().length === 0);
      quotaBlocked = wholeMarket && !!market && q.used >= q.limit;
      $("quota-block").hidden = !quotaBlocked;
      $("quota-block-text").textContent = quotaBlocked ? refusal(q) : "";
      refreshStart();
    };

    var times = function (list) {
      return (list || []).map(function (t) { return String(t).substr(11, 5); }).filter(function (t) { return t; }).join(", ");
    };

    // the text-file rules and the daily limit per stock
    var showLimits = function () {
      var mode = currentMode();
      var title = "", text = "";
      if (mode === "file" && fileQuota.used >= fileQuota.limit) {
        title = "Daily limit reached.";
        text = "loading from a text file may run at most " + fileQuota.limit + " times a day - it already ran " +
               fileQuota.used + " time(s) today" + (times(fileQuota.started) ? " (at " + times(fileQuota.started) + ")" : "") +
               ". Try again tomorrow, or resume an unfinished run.";
      } else if (mode === "file" && fileSymbols && fileSymbols.length > maxFileSymbols) {
        title = "Too many stock symbols.";
        text = fileName + " lists " + fileSymbols.length.toLocaleString("en-US") + " stock symbols - a text file may list at most " +
               maxFileSymbols + " (split it into several files).";
      }
      fileBlocked = !!title;
      $("file-block").hidden = !fileBlocked;
      $("file-block-title").textContent = title;
      $("file-block-text").textContent = text;
      $("file-count").textContent = mode === "file" && fileSymbols ?
        "= " + fileSymbols.length.toLocaleString("en-US") + " stock symbol(s) in " + fileName : "";

      var symbols = mode === "symbols" ? parseSymbols($("symbols").value).symbols :
                    mode === "file" ? (fileSymbols || []) : mode === "bulk" ? bulkSymbols() : [];
      var tickers = [];
      symbols.forEach(function (s) { var t = tickerOf(s); if (tickers.indexOf(t) < 0) { tickers.push(t); } });
      var limited = tickers.filter(function (t) { return (atLimit[t] || 0) >= stockLimit; });
      var notice = $("stock-notice");
      stocksBlocked = limited.length > 0 && limited.length === tickers.length;
      notice.hidden = limited.length === 0;
      notice.className = "flash " + (stocksBlocked ? "error" : "warning");
      notice.setAttribute("role", stocksBlocked ? "alert" : "status");
      var names = limited.slice(0, 20).join(", ") + (limited.length > 20 ? " and " + (limited.length - 20) + " more" : "");
      $("stock-notice-title").textContent = stocksBlocked ? "Nothing to load." : "Daily limit per stock.";
      // one day, all stocks: no weekend, nothing in the future (holidays are checked by the server)
      var bulkText = "";
      if (mode === "bulk" && $("bulk_date").value) {
        var parts = $("bulk_date").value.split("-").map(Number);
        var day = new Date(parts[0], parts[1] - 1, parts[2]);
        var today = new Date(); today.setHours(0, 0, 0, 0);
        if (day > today) {
          bulkText = $("bulk_date").value + " is in the future - there is no end-of-day data for it yet.";
        } else if ((day.getDay() === 0 || day.getDay() === 6) && !$("bulk_force").checked) {
          bulkText = bulkMarket() + " does not trade on " + $("bulk_date").value + " (a " +
                     (day.getDay() === 0 ? "Sunday" : "Saturday") + ") - the API has no data for that day. " +
                     "Choose a trading day, or tick 'call the API even if the exchange does not trade that day'.";
        }
      }
      bulkBlocked = !!bulkText;
      $("bulk-notice").hidden = !bulkBlocked;
      $("bulk-notice-text").textContent = bulkText;
      $("stock-notice-text").textContent = !limited.length ? "" : stocksBlocked ?
        (limited.length === 1 ? names + " was" : names + " were") + " already loaded " + stockLimit +
        " times today - a stock may be loaded at most " + stockLimit + " times a day. Try again tomorrow." :
        "already loaded " + stockLimit + " times today - " + limited.length + " stock(s) will be skipped (not called): " + names;
      refreshStart();
    };

    // the calls of the bulk endpoint for the exchange and trading day chosen (asked from the server)
    var showBulkCalls = function () {
      var q = bulkQuota || {};
      var shown = currentMode() === "bulk" && q.market;
      $("bulk-quota-count").textContent = q.market ? q.market + " " + q.trade_date + ": " + q.used + " of " +
                                                     q.limit + " used" : "";
      callsBlocked = !!(shown && q.used >= q.limit);
      $("bulk-call-block").hidden = !callsBlocked;
      $("bulk-call-block-text").textContent = callsBlocked ?
        q.market + " " + q.trade_date + ": the bulk endpoint may be called at most " + q.limit +
        " times for the same market and day - it was already called " + q.used + " time(s) for it. " +
        "Choose another day, or use the data already in the staging table." : "";
      refreshStart();
    };
    var askBulkCalls = function () {
      if (!bulkQuotaUrl || !window.fetch) { return; }
      window.clearTimeout(bulkQuotaTimer);
      bulkQuotaTimer = window.setTimeout(function () {
        var url = bulkQuotaUrl + "?market=" + encodeURIComponent(bulkMarket()) +
                  "&date=" + encodeURIComponent($("bulk_date").value || "");
        fetch(url, {headers: {"Accept": "application/json"}})
          .then(function (r) { return r.json(); })
          .then(function (q) { bulkQuota = q.error ? {} : q; showBulkCalls(); })
          .catch(function () { /* the server checks anyway */ });
      }, 250);
    };

    var showMode = function () {
      var mode = currentMode();
      ["symbols", "market", "file", "bulk"].forEach(function (m) {
        form.querySelectorAll(".mode-" + m).forEach(function (el) { el.hidden = m !== mode; });
      });
      form.querySelectorAll(".not-bulk").forEach(function (el) { el.hidden = mode === "bulk"; });
      form.querySelectorAll(".mode-tabs label").forEach(function (label) {
        label.classList.toggle("active", label.querySelector("input").value === mode);
      });
      $("symbols").required = mode === "symbols";
      $("file").required = mode === "file";
      showQuota();
      showLimits();
      showBulkCalls();
    };

    var currentMarket = function () {
      var select = $("market");
      return (select && select.value) || ($("market_other").value || "").trim().toUpperCase();
    };

    var countSelected = function () {
      var info = lists.filter(function (l) { return l.market === currentMarket(); })[0];
      var out = $("market-count");
      if (!info) { out.textContent = ""; return; }
      var all = $("all_types").checked, n = 0;
      Object.keys(info.types).forEach(function (t) {
        var box = typesBox.querySelector("input[value='" + t.replace(/'/g, "\\'") + "']");
        if (all || (box && box.checked)) { n += info.types[t]; }
      });
      var limit = parseInt($("limit").value, 10);
      out.textContent = "= " + (limit > 0 ? Math.min(limit, n) : n).toLocaleString("en-US") + " stock(s), one API call each";
    };

    var showTypes = function () {
      var info = lists.filter(function (l) { return l.market === currentMarket(); })[0];
      typesBox.querySelectorAll("label.type").forEach(function (el) { el.remove(); });
      $("types-none").hidden = !!info;
      if (info) {
        Object.keys(info.types).forEach(function (t) {
          var label = document.createElement("label");
          label.className = "type";
          var box = document.createElement("input");
          box.type = "checkbox";
          box.name = "types";
          box.value = t;
          box.checked = chosen ? chosen.indexOf(t) >= 0 : defaults.indexOf(t) >= 0;
          box.disabled = $("all_types").checked;
          box.addEventListener("change", function () {
            chosen = Array.prototype.map.call(typesBox.querySelectorAll("input[name=types]:checked"),
                                              function (b) { return b.value; });
            countSelected();
          });
          label.appendChild(box);
          label.appendChild(document.createTextNode(" " + t + " (" + info.types[t].toLocaleString("en-US") + ")"));
          typesBox.appendChild(label);
        });
      }
      $("market_other").hidden = !!($("market") && $("market").value);
      countSelected();
      showQuota();
    };

    form.querySelectorAll("input[name=mode]").forEach(function (r) { r.addEventListener("change", showMode); });
    if ($("market")) { $("market").addEventListener("change", showTypes); }
    $("market_other").addEventListener("input", showTypes);
    $("all_types").addEventListener("change", function () {
      typesBox.querySelectorAll("input[name=types]").forEach(function (b) { b.disabled = $("all_types").checked; });
      countSelected();
    });
    $("limit").addEventListener("input", countSelected);
    $("symbols").addEventListener("input", showLimits);
    ["bulk_market", "bulk_symbols"].forEach(function (id) {
      $(id).addEventListener("input", function () { showQuota(); showLimits(); });
    });
    ["bulk_date", "bulk_force"].forEach(function (id) { $(id).addEventListener("change", showLimits); });
    $("bulk_market").addEventListener("input", askBulkCalls);
    $("bulk_date").addEventListener("change", askBulkCalls);
    $("file").addEventListener("change", function () {
      var file = $("file").files && $("file").files[0];
      fileSymbols = null;
      fileName = file ? file.name : "";
      showLimits();
      if (!file || !window.FileReader || file.size > 1024 * 1024) { return; }   // too big: the server says so
      var reader = new FileReader();
      reader.onload = function () {
        fileSymbols = parseSymbols(reader.result).symbols;
        showLimits();
      };
      reader.readAsText(file);
    });
    form.addEventListener("submit", function (event) {
      showQuota();
      showLimits();
      if (quotaBlocked || fileBlocked || stocksBlocked || bulkBlocked || callsBlocked) {   // the server refuses it too
        event.preventDefault();
        var shown = [$("quota-block"), $("file-block"), $("bulk-call-block"), $("bulk-notice"), $("stock-notice")]
          .filter(function (el) { return !el.hidden; })[0];
        if (shown) { shown.scrollIntoView({block: "center"}); }
      }
    });
    showMode();
    showTypes();
    if ($("bulk_date").value || bulkMarket() !== "US") { askBulkCalls(); }   // a form kept from before
  }

  // -- the running load ---------------------------------------------------------------------
  var card = $("job-card");
  if (!card) { return; }
  var statusUrl = card.getAttribute("data-status-url");
  var runUrl = card.getAttribute("data-run-url");
  var fmt = function (n) { return Number(n || 0).toLocaleString("en-US"); };
  var wasActive = card.getAttribute("data-active") === "1";

  var render = function (data) {
    var job = data.job;
    $("job-empty").hidden = !!job;
    $("job-body").hidden = !job;
    if (!job) { return; }
    $("job-title").textContent = job.title;
    var badge = $("job-status");
    badge.textContent = job.status + (job.stop_requested && job.active ? " (stopping)" : "");
    badge.className = "badge " + (job.active ? "warn" : job.status === "failed" ? "err" :
                                  job.run_status === "completed" ? "ok" : "err");
    $("job-run-status").textContent = job.run_status || "";
    var run = $("job-run");
    while (run.firstChild) { run.removeChild(run.firstChild); }
    if (job.run_id) {
      var a = document.createElement("a");
      a.href = runUrl.replace("RUN_ID", job.run_id);
      a.className = "mono";
      a.textContent = job.run_id;
      run.appendChild(a);
    }
    $("job-bar").style.width = job.percent + "%";
    $("job-done").textContent = fmt(job.done);
    $("job-total").textContent = fmt(job.total);
    $("job-loaded").textContent = fmt(job.loaded);
    $("job-rows").textContent = fmt(job.rows);
    $("job-nodata").textContent = fmt(job.no_data);
    $("job-failed").textContent = fmt(job.failed);
    $("job-skipped").textContent = fmt(job.skipped);
    $("job-calls").textContent = fmt(job.api_calls);
    var log = $("job-log"), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    while (log.firstChild) { log.removeChild(log.firstChild); }
    job.log.forEach(function (line) {
      var div = document.createElement("div");
      div.className = "log-" + line.level;
      var at = document.createElement("span");
      at.className = "muted";
      at.textContent = line.at + " ";
      div.appendChild(at);
      div.appendChild(document.createTextNode(line.text));
      log.appendChild(div);
    });
    if (atBottom) { log.scrollTop = log.scrollHeight; }
    var stop = $("stop-form");
    if (stop) { stop.hidden = !job.active; }
    loadBusy = !!data.busy;
    refreshStart();
    document.querySelectorAll(".resume-form button").forEach(function (b) { b.disabled = data.busy; });
  };

  var poll = function () {
    fetch(statusUrl + "?log=150", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.json(); })
      .then(function (data) {
        render(data);
        if (data.busy) {
          wasActive = true;
          window.setTimeout(poll, 1500);
        } else if (wasActive) {
          window.setTimeout(function () { window.location.reload(); }, 800);   // the runs table is new
        }
      })
      .catch(function () { window.setTimeout(poll, 5000); });
  };
  var log = $("job-log");
  if (log) { log.scrollTop = log.scrollHeight; }
  if (wasActive) { window.setTimeout(poll, 800); }
})();
