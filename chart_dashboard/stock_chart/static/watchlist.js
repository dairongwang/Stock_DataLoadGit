/*
 * watchlist.js - the list of stocks a user steps through on the chart page.
 *
 * Pure functions (no DOM), exported for the page (window.Watchlist) and for unit tests
 * (node: require("./watchlist.js")).  The list is an array of upper-case tickers, kept per
 * browser in localStorage; the page supplies the default list from [chart] watchlist.
 *
 *   Watchlist.parse("hd, jnj;JPM")            -> ["HD", "JNJ", "JPM"]
 *   Watchlist.add(list, "pfe")                -> {list: [..., "PFE"], added: "PFE", error: null}
 *   Watchlist.remove(list, "HD")              -> the list without HD
 *   Watchlist.neighbour(list, "JNJ", +1)      -> the ticker after JNJ (null at the end)
 *   Watchlist.load(storage, defaults) / Watchlist.save(storage, list)
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.Watchlist = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var KEY = "stock_chart.watchlist";
  var MAX = 200;
  var TICKER_RE = /^[A-Z0-9.\-^=_]{1,20}$/;

  /** " mrk " -> "MRK"; null when it is not a ticker. */
  function normalize(text) {
    var t = String(text === null || text === undefined ? "" : text).trim().toUpperCase();
    return TICKER_RE.test(t) ? t : null;
  }

  /** "hd, jnj;JPM hd" -> ["HD", "JNJ", "JPM"]: valid tickers, unique, in order (invalid ones are left out). */
  function parse(text) {
    var out = [];
    String(text || "").split(/[\s,;]+/).forEach(function (part) {
      var t = normalize(part);
      if (t && out.indexOf(t) < 0) { out.push(t); }
    });
    return out;
  }

  /** A copy of the list with *ticker* appended: {list, added, error} - error when invalid, present or full. */
  function add(list, ticker, max) {
    var t = normalize(ticker);
    var limit = max || MAX;
    if (!t) { return {list: list.slice(), added: null, error: "'" + String(ticker).trim() + "' is not a valid ticker"}; }
    if (list.indexOf(t) >= 0) { return {list: list.slice(), added: null, error: t + " is already in the list"}; }
    if (list.length >= limit) { return {list: list.slice(), added: null, error: "the list is full (" + limit + " tickers)"}; }
    return {list: list.concat([t]), added: t, error: null};
  }

  /** A copy of the list without *ticker*. */
  function remove(list, ticker) {
    var t = normalize(ticker);
    return list.filter(function (x) { return x !== t; });
  }

  /** The ticker *step* places after (+1) / before (-1) *current*; null past either end.  When *current*
   *  is not in the list, +1 gives the first and -1 the last ticker. */
  function neighbour(list, current, step) {
    if (!list.length) { return null; }
    var i = list.indexOf(normalize(current));
    if (i < 0) { return step > 0 ? list[0] : list[list.length - 1]; }
    var j = i + step;
    return j >= 0 && j < list.length ? list[j] : null;
  }

  /** {index (0-based, -1 = not in the list), total} */
  function position(list, current) {
    return {index: list.indexOf(normalize(current)), total: list.length};
  }

  /** The stored list, else *defaults* (cleaned).  *storage*: localStorage or anything with getItem. */
  function load(storage, defaults) {
    var fallback = parse((defaults || []).join(",")).slice(0, MAX);
    if (!storage) { return fallback; }
    try {
      var raw = storage.getItem(KEY);
      if (raw === null || raw === undefined) { return fallback; }
      var value = JSON.parse(raw);
      if (!Array.isArray(value)) { return fallback; }
      return parse(value.join(",")).slice(0, MAX);
    } catch (ignore) {
      return fallback;                              // unreadable entry or storage blocked
    }
  }

  /** Store the list; false when the browser does not allow it (the list then lives until the page closes). */
  function save(storage, list) {
    if (!storage) { return false; }
    try {
      storage.setItem(KEY, JSON.stringify(list));
      return true;
    } catch (ignore) {
      return false;
    }
  }

  return {KEY: KEY, MAX: MAX, normalize: normalize, parse: parse, add: add, remove: remove,
          neighbour: neighbour, position: position, load: load, save: save};
});
