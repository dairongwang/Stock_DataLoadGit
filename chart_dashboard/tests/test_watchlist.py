"""The watchlist: /api/quotes, the default list ([chart] watchlist) and the pure functions of
static/watchlist.js (run with Node.js - skipped without it).  The page itself: test_browser."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stock_chart.app import create_app
from stock_chart.config import APP_ROOT, ConfigError, load_settings, parse_watchlist
from stock_chart.repository import DailyRepository, InvalidRequest

from .helpers import ChartTestCase

SCRIPT = APP_ROOT / "stock_chart" / "static" / "watchlist.js"
NODE = shutil.which("node")


class QuoteTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.repo = DailyRepository(self.settings)
        self.client = create_app(self.settings).test_client()

    def test_latest_close_and_change(self):
        quotes, unknown = self.repo.quotes(["aab", "AAA", "NOPE", "aaa"])
        self.assertEqual([q["ticker"] for q in quotes], ["AAB", "AAA"])                # the order asked for
        self.assertEqual(unknown, ["NOPE"])
        last, before = self.data["AAB"][-1], self.data["AAB"][-2]
        q = quotes[0]
        self.assertEqual((q["market"], q["date"], q["close"], q["previous_close"]), ("NYSE", last[1], last[5], before[5]))
        self.assertAlmostEqual(q["change"], last[5] - before[5])
        self.assertAlmostEqual(q["change_pct"], (last[5] - before[5]) / before[5] * 100, places=3)
        with sqlite3.connect(self.db) as conn:                                          # one day only: no change
            conn.execute('INSERT INTO "Stock_Daily" ("Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume") '
                         "VALUES ('ONE', '2025-01-02', 1, 2, 0.5, 1.5, 10)")
        one = self.repo.quotes(["ONE"])[0][0]
        self.assertEqual((one["close"], one["previous_close"], one["change"], one["change_pct"]), (1.5, None, None, None))
        self.assertEqual(self.repo.quotes([]), ([], []))
        with self.assertRaises(InvalidRequest):
            self.repo.quotes(["A A"])

    def test_api(self):
        body = self.client.get("/api/quotes?tickers=AAA,zzz,NOPE").get_json()
        self.assertEqual((body["count"], [q["ticker"] for q in body["quotes"]], body["unknown"]), (2, ["AAA", "ZZZ"], ["NOPE"]))
        self.assertIsNone(body["quotes"][1]["market"])
        for url in ("/api/quotes", "/api/quotes?tickers=A%20A", "/api/quotes?tickers=" + ",".join(["AAA"] * 1001)):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 400, url)
            self.assertIn("error", r.get_json())
        self.settings.daily_table = "Missing_Table"
        self.assertEqual(create_app(self.settings).test_client().get("/api/quotes?tickers=AAA").status_code, 503)


class DefaultListTests(ChartTestCase):
    def page(self, url: str = "/") -> str:
        return create_app(self.settings).test_client().get(url).get_data(as_text=True)

    def test_every_ticker_unless_configured(self):
        page = self.page()
        self.assertIn('data-watchlist="AAA,AAB,ZZZ"', page)
        self.assertIn('data-ticker-from-url=""', page)
        self.assertIn('data-ticker-from-url="1"', self.page("/?ticker=AAB"))
        for element in ('id="wl-list"', 'id="wl-select"', 'id="wl-prev"', 'id="wl-next"', 'id="wl-add"', "watchlist.js"):
            self.assertIn(element, page)
        self.settings.watchlist = ("ZZZ", "AAA")
        self.assertIn('data-watchlist="ZZZ,AAA"', self.page())

    def test_configuration(self):
        self.assertEqual(parse_watchlist("hd, JNJ; jpm  hd"), ("HD", "JNJ", "JPM"))
        self.assertEqual(parse_watchlist(""), ())
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        ini = folder / "chart.ini"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_CHART_PASSWORD", None)
            ini.write_text("[chart]\nwatchlist = pfe, HD\n", encoding="utf-8")
            self.assertEqual(load_settings(ini).watchlist, ("PFE", "HD"))
            self.assertEqual(load_settings(APP_ROOT / "chart.ini").watchlist, ())      # shipped: every ticker
            for bad in ("[chart]\nwatchlist = A/B\n", "[chart]\nwatchlist = " + ",".join(f"T{i}" for i in range(201)) + "\n"):
                ini.write_text(bad, encoding="utf-8")
                with self.assertRaises(ConfigError, msg=bad[:40]):
                    load_settings(ini)


def run_js(code: str):
    prog = f"const W = require({json.dumps(str(SCRIPT))});\n{code}"
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node.js is not installed")
class WatchlistFunctionTests(unittest.TestCase):
    def test_parse_add_remove(self):
        r = run_js("""const l = ["HD", "JNJ"];
            console.log(JSON.stringify({
              parse: W.parse(" hd, jnj;JPM  hd bad/one "), norm: [W.normalize(" mrk "), W.normalize("A A")],
              add: W.add(l, "pfe"), dup: W.add(l, "hd"), bad: W.add(l, "x y"), full: W.add(l, "PFE", 2),
              remove: W.remove(l, "hd"), untouched: l}));""")
        self.assertEqual(r["parse"], ["HD", "JNJ", "JPM"])
        self.assertEqual(r["norm"], ["MRK", None])
        self.assertEqual(r["add"], {"list": ["HD", "JNJ", "PFE"], "added": "PFE", "error": None})
        self.assertEqual((r["dup"]["added"], r["dup"]["error"]), (None, "HD is already in the list"))
        self.assertIn("not a valid ticker", r["bad"]["error"])
        self.assertIn("full", r["full"]["error"])
        self.assertEqual((r["remove"], r["untouched"]), (["JNJ"], ["HD", "JNJ"]))      # copies, never in place

    def test_up_and_down(self):
        r = run_js("""const l = ["HD", "JNJ", "JPM"];
            console.log(JSON.stringify({
              down: W.neighbour(l, "HD", 1), up: W.neighbour(l, "jnj", -1), top: W.neighbour(l, "HD", -1),
              bottom: W.neighbour(l, "JPM", 1), outDown: W.neighbour(l, "MRK", 1), outUp: W.neighbour(l, "MRK", -1),
              empty: W.neighbour([], "HD", 1), pos: W.position(l, "JPM"), out: W.position(l, "MRK")}));""")
        self.assertEqual((r["down"], r["up"], r["top"], r["bottom"]), ("JNJ", "HD", None, None))
        self.assertEqual((r["outDown"], r["outUp"], r["empty"]), ("HD", "JPM", None))
        self.assertEqual((r["pos"], r["out"]), ({"index": 2, "total": 3}, {"index": -1, "total": 3}))

    def test_storage(self):
        r = run_js("""const store = {}, mem = {getItem: k => (k in store ? store[k] : null),
                                               setItem: (k, v) => { store[k] = v; }};
            const first = W.load(mem, ["hd", "JNJ", "hd"]);
            const saved = W.save(mem, ["PFE", "MRK"]);
            const again = W.load(mem, ["HD"]);
            store[W.KEY] = "not json";
            const broken = W.load(mem, ["HD"]);
            const blocked = {getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); }};
            console.log(JSON.stringify({first, saved, again, broken, none: W.load(null, ["JPM"]),
                                        blockedLoad: W.load(blocked, ["JPM"]), blockedSave: W.save(blocked, ["X"])}));""")
        self.assertEqual((r["first"], r["saved"], r["again"], r["broken"]), (["HD", "JNJ"], True, ["PFE", "MRK"], ["HD"]))
        self.assertEqual((r["none"], r["blockedLoad"], r["blockedSave"]), (["JPM"], ["JPM"], False))

    def test_scripts_parse(self):
        subprocess.run([NODE, "--check", str(SCRIPT)], check=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
