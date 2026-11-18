"""The chart page in a real browser (Playwright + headless Chromium; skipped when not available)."""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path

from .helpers import ChartTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/pw-browsers").is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"


def _browser_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


BROWSER_OK = _browser_available()


@unittest.skipUnless(BROWSER_OK, "playwright / chromium not available")
class BrowserTests(ChartTestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        from stock_chart.app import create_app
        self.server = make_server("127.0.0.1", 0, create_app(self.settings), threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.page = self._browser.new_page(viewport={"width": 1300, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)

    def tearDown(self):
        self.page.close()
        self.server.shutdown()
        super().tearDown()

    def candles(self) -> int:
        """Candles drawn = sub-paths of the two body paths."""
        return self.page.evaluate("""() => Array.from(document.querySelectorAll('.cc-body'))
            .reduce((n, p) => n + (p.getAttribute('d').match(/Z/g) || []).length, 0)""")

    def test_candles_volume_and_legend(self):
        pg = self.page
        pg.goto(self.base + "/?ticker=AAA&range=ALL")
        pg.wait_for_selector(".cc-body")
        self.assertEqual(self.candles(), 300)
        volume = pg.evaluate("""() => Array.from(document.querySelectorAll('.cc-volume'))
            .reduce((n, p) => n + (p.getAttribute('d').match(/Z/g) || []).length, 0)""")
        self.assertEqual(volume, 300)
        self.assertEqual(pg.inner_text("#s-days"), "300")
        last = self.data["AAA"][-1]
        legend = pg.inner_text(".cc-legend")
        self.assertIn(last[1], legend)                                    # the newest day by default
        self.assertIn(f"C {last[5]:,.2f}", legend)
        box = pg.locator(".cc-svg").bounding_box()
        pg.mouse.move(box["x"] + 9, box["y"] + 100)                       # hover the first candle (left edge)
        self.assertIn(self.data["AAA"][0][1], pg.inner_text(".cc-legend"))
        self.assertEqual(pg.locator(".cc-cross").count(), 2)
        self.assertEqual(self.errors, [])

    def test_ranges_zoom_search_and_url(self):
        pg = self.page
        pg.goto(self.base + "/?ticker=AAA&range=1M")
        pg.wait_for_selector(".cc-body")
        self.assertTrue(19 <= self.candles() <= 23)
        pg.click("[data-range-button='3M']")
        self.assertIn("range=3M", pg.url)
        self.assertTrue(60 <= self.candles() <= 66)
        box = pg.locator(".cc-svg").bounding_box()
        pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + 150)
        pg.mouse.wheel(0, 400)                                             # zoom out
        pg.wait_for_timeout(100)
        self.assertGreater(self.candles(), 66)
        self.assertEqual(pg.locator("[data-range-button].active").count(), 0)
        pg.dblclick(".cc-svg")                                             # back to 3M
        self.assertEqual(pg.locator("[data-range-button='3M'].active").count(), 1)
        pg.check("[data-ma='20']")
        self.assertEqual(pg.locator(".cc-ma-20").count(), 1)
        pg.fill("#ticker", "aab")
        pg.click("header button")
        pg.locator("#title-ticker", has_text="AAB").wait_for()          # (the page's CSP forbids eval-based waits)
        self.assertIn("ticker=AAB", pg.url)
        self.assertIn("NYSE", pg.inner_text("#title-meta"))
        pg.fill("#ticker", "NOPE")
        pg.click("header button")
        pg.wait_for_selector(".status.error")
        self.assertIn("no daily data for NOPE", pg.inner_text("#status"))
        self.assertEqual([e for e in self.errors if "404" not in e], [])

    def test_weekly_and_monthly_candles(self):
        from .helpers import expected_periods
        weeks = expected_periods(self.data["AAA"], "weekly")
        months = expected_periods(self.data["AAA"], "monthly")
        pg = self.page
        pg.goto(self.base + "/?ticker=AAA&range=ALL")
        pg.wait_for_selector(".cc-body")
        pg.click("[data-interval-button='weekly']")
        pg.locator("#title-meta", has_text="weeks").wait_for()
        self.assertEqual(self.candles(), len(weeks))
        self.assertIn("interval=weekly", pg.url)
        self.assertEqual(pg.inner_text("#s-days"), str(len(weeks)))
        self.assertEqual(pg.inner_text("#l-days").lower(), "weeks")
        self.assertIn("Stock_Daily (aggregated)", pg.inner_text("#source"))
        legend = pg.inner_text(".cc-legend")                                  # the newest week
        self.assertIn("Week ", legend)
        self.assertIn(f"C {weeks[-1][4]:,.2f}", legend)
        self.assertIn(f"{weeks[-1][8]} trading day", legend)
        self.assertIn("/api/weekly/AAA?format=csv", pg.get_attribute("#csv", "href"))
        pg.click("[data-interval-button='monthly']")
        pg.locator("#title-meta", has_text="months").wait_for()
        self.assertEqual(self.candles(), len(months))
        self.assertEqual(pg.locator("[data-interval-button='monthly'].active").count(), 1)
        self.assertIn(f"C {months[-1][4]:,.2f}", pg.inner_text(".cc-legend"))
        pg.reload()                                                            # the URL keeps the interval
        pg.locator("#title-meta", has_text="months").wait_for()
        self.assertEqual(self.candles(), len(months))
        pg.click("[data-interval-button='daily']")
        pg.locator("#title-meta", has_text="trading days").wait_for()
        self.assertEqual(self.candles(), 300)
        self.assertEqual(self.errors, [])

    def listed(self) -> list:
        return self.page.eval_on_selector_all("#wl-list li[data-ticker]", "els => els.map(e => e.dataset.ticker)")

    def showing(self, ticker: str) -> None:
        self.page.locator("#title-ticker", has_text=ticker).wait_for()
        self.page.locator("#wl-list li.active", has_text=ticker).wait_for()

    def test_watchlist_up_down_go_to_add_and_remove(self):
        pg = self.page
        pg.goto(self.base + "/")
        pg.wait_for_selector(".wl-price:not(:empty)")
        self.assertEqual(self.listed(), ["AAA", "AAB", "ZZZ"])                # the default: every ticker
        self.showing("AAA")                                                    # the first of the list
        self.assertEqual(pg.inner_text("#wl-pos"), "1 / 3")
        self.assertTrue(pg.is_disabled("#wl-prev"))
        self.assertIn(f"{self.data['AAA'][-1][5]:,.2f}", pg.inner_text("#wl-list li.active"))   # the latest close
        # down and up: the buttons and the arrow keys
        pg.click("#wl-next")
        self.showing("AAB")
        self.assertIn("ticker=AAB", pg.url)
        pg.focus("#wl-list")
        pg.keyboard.press("ArrowDown")
        self.showing("ZZZ")
        self.assertTrue(pg.is_disabled("#wl-next"))
        self.assertEqual(pg.inner_text("#wl-pos"), "3 / 3")
        pg.keyboard.press("ArrowUp")
        self.showing("AAB")
        pg.click("#wl-prev")
        self.showing("AAA")
        # go to an item from the drop-down
        pg.select_option("#wl-select", "ZZZ")
        self.showing("ZZZ")
        self.assertEqual(pg.eval_on_selector("#wl-select", "s => s.value"), "ZZZ")
        # remove / add (only tickers with data, no duplicates)
        pg.click("button[data-remove='AAB']")
        pg.locator("#wl-msg", has_text="removed AAB").wait_for()
        self.assertEqual(self.listed(), ["AAA", "ZZZ"])
        pg.fill("#wl-add", "nope")
        pg.click("#wl-add-form button")
        pg.locator("#wl-msg.error", has_text="no data for NOPE").wait_for()
        pg.fill("#wl-add", "aaa")
        pg.click("#wl-add-form button")
        pg.locator("#wl-msg.error", has_text="AAA is already in the list").wait_for()
        pg.fill("#wl-add", "aab")
        pg.click("#wl-add-form button")
        self.showing("AAB")                                                    # added at the end and shown
        self.assertEqual(self.listed(), ["AAA", "ZZZ", "AAB"])
        # the list is saved in the database
        pg.goto(self.base + "/")
        pg.wait_for_selector(".wl-price:not(:empty)")
        self.assertEqual(self.listed(), ["AAA", "ZZZ", "AAB"])
        # a stock that is not in the list: the title button adds it, and removes it again
        pg.click("button[data-remove='AAA']")
        pg.locator("#wl-msg", has_text="removed AAA").wait_for()
        pg.fill("#ticker", "aaa")
        pg.click("header button")
        pg.locator("#wl-toggle", has_text="Add to watchlist").wait_for()
        self.assertEqual(pg.inner_text("#wl-pos"), "– / 2")
        pg.click("#wl-toggle")
        pg.locator("#wl-toggle.in").wait_for()
        self.assertEqual(self.listed(), ["ZZZ", "AAB", "AAA"])
        pg.click("#wl-toggle")
        pg.locator("#wl-toggle:not(.in)").wait_for()
        # reset: the default list is back
        pg.once("dialog", lambda d: d.accept())
        pg.click("#wl-reset")
        pg.locator("#wl-msg", has_text="default list").wait_for()
        self.assertEqual(self.listed(), ["AAA", "AAB", "ZZZ"])
        self.assertEqual(self.errors, [])

    def choose_list(self, label_start: str) -> None:
        pg = self.page
        value = pg.eval_on_selector_all("#list-select option", f"""os => os.filter(o => o.textContent
            .startsWith({label_start!r})).map(o => o.value)""")[0]
        pg.select_option("#list-select", value)
        pg.locator("#list-name", has_text=label_start).wait_for()

    def test_screen_list_selected_list_and_going_to_a_symbol(self):
        import sqlite3
        screen = self.root / "my screen.txt"
        screen.write_text("zzz, AAB,NOPE , bad/one,aaa", encoding="utf-8")
        pg = self.page
        pg.goto(self.base + "/")
        pg.wait_for_selector("#wl-list li[data-ticker]")
        # load the screen list from the text file: it becomes the list on show, its first stock is opened
        pg.set_input_files("#screen-file", str(screen))
        pg.locator("#wl-msg", has_text="loaded screen list my screen: 4 symbols").wait_for()
        self.assertIn("1 without data: NOPE", pg.inner_text("#wl-msg"))
        self.assertIn("not a ticker: bad/one", pg.inner_text("#wl-msg"))
        self.showing("ZZZ")
        self.assertEqual(self.listed(), ["ZZZ", "AAB", "NOPE", "AAA"])
        self.assertEqual(pg.inner_text("#list-name"), "Screen list: my screen")
        self.assertIn("no data", pg.inner_text("#wl-list li[data-ticker='NOPE']"))
        # down the list, then go to a symbol by typing it, and by the drop-down
        pg.click("#wl-next")
        self.showing("AAB")
        pg.fill("#wl-goto", "aaa")
        pg.press("#wl-goto", "Enter")
        self.showing("AAA")
        self.assertEqual(pg.inner_text("#wl-pos"), "4 / 4")
        pg.select_option("#wl-select", "AAB")
        self.showing("AAB")
        # like it: add the stock on show to the Selected list
        pg.click("#sel-toggle")
        pg.locator("#sel-toggle.in").wait_for()
        pg.click("#wl-prev")
        self.showing("ZZZ")
        self.assertEqual(pg.inner_text("#sel-toggle"), "+ Add to Selected")
        pg.click("#sel-toggle")
        pg.locator("#sel-toggle.in").wait_for()
        # review the Selected list by its charts; remove a stock from it
        self.choose_list("Selected")
        self.assertEqual(self.listed(), ["AAB", "ZZZ"])
        self.assertIn("list=", pg.url)
        self.assertEqual(pg.inner_text("#wl-pos"), "2 / 2")             # ZZZ, on show, is in the list
        self.assertTrue(pg.is_disabled("#wl-next"))
        pg.click("button[data-remove='AAB']")
        pg.locator("#wl-msg", has_text="removed AAB from Selected").wait_for()
        self.assertEqual(self.listed(), ["ZZZ"])
        pg.fill("#wl-add", "aaa")
        pg.click("#wl-add-form button")
        self.showing("AAA")
        # everything is in the database: a reload shows the same lists, the Selected list still on show
        pg.goto(self.base + "/")
        pg.locator("#list-name", has_text="Selected").wait_for()
        pg.wait_for_selector("#wl-list li[data-ticker]")
        self.assertEqual(self.listed(), ["ZZZ", "AAA"])
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute('SELECT l."Kind", l."Name", i."Ticker" FROM "Chart_List" l JOIN "Chart_List_Item" i '
                                'USING ("ListId") WHERE l."Kind" <> \'watch\' ORDER BY l."Kind", i."Position"').fetchall()
        self.assertEqual(rows, [("screen", "my screen", "ZZZ"), ("screen", "my screen", "AAB"), ("screen", "my screen", "NOPE"),
                                ("screen", "my screen", "AAA"), ("selected", "Selected", "ZZZ"), ("selected", "Selected", "AAA")])
        # a screen list can be deleted
        self.choose_list("my screen")
        pg.once("dialog", lambda d: d.accept())
        pg.click("#list-delete")
        pg.locator("#wl-msg", has_text="deleted the screen list my screen").wait_for()
        pg.locator("#list-name", has_text="Watchlist").wait_for()
        self.assertEqual(pg.eval_on_selector_all("#list-select option", "os => os.map(o => o.textContent)"),
                         ["Watchlist (3)", "Selected (2)"])
        self.assertEqual(self.errors, [])

    def test_a_watchlist_kept_by_the_browser_moves_into_the_database(self):
        pg = self.page
        pg.add_init_script("if (!sessionStorage.getItem('seeded')) { sessionStorage.setItem('seeded', '1'); "
                           "localStorage.setItem('stock_chart.watchlist', JSON.stringify(['ZZZ', 'AAB'])); }")
        pg.goto(self.base + "/")
        pg.locator("#wl-msg", has_text="moved into the database").wait_for()
        self.assertEqual(self.listed(), ["ZZZ", "AAB"])
        self.assertIsNone(pg.evaluate("localStorage.getItem('stock_chart.watchlist')"))
        pg.goto(self.base + "/")
        pg.wait_for_selector("#wl-list li[data-ticker]")
        self.assertEqual(self.listed(), ["ZZZ", "AAB"])
        self.assertEqual(self.errors, [])

    def test_empty_table(self):
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            conn.execute('DELETE FROM "Stock_Daily"')
        pg = self.page
        pg.goto(self.base + "/")
        pg.wait_for_selector(".cc-empty")
        self.assertIn("no rows yet", pg.inner_text("#status"))
        self.assertFalse(pg.is_visible("#csv"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
