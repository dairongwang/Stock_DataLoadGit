"""
Real-browser tests of the dashboard (Playwright + headless Chromium).

They start the Flask app on an ephemeral port in a background thread and
click through it like a business user would.  Skipped automatically when
Playwright or a Chromium build is not available:

    pip install playwright && playwright install chromium
    # or point PLAYWRIGHT_BROWSERS_PATH at an existing browser cache
"""

from __future__ import annotations

import os
import shutil
import threading
import unittest
from pathlib import Path

from stock_dataload.config import load_settings, save_settings
from stock_dataload.db import StageDatabase

from .helpers import ORIGINAL, SAMPLES, TempProjectTestCase, register_listed

ORIGINAL_TEXT = ORIGINAL.read_text(encoding="utf-8")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

# a known browser cache in this environment (harmless elsewhere)
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/pw-browsers").is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"


def _browser_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


BROWSER_OK = _browser_available()


@unittest.skipUnless(BROWSER_OK, "playwright/chromium not available")
class BrowserDashboardTests(TempProjectTestCase):
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
        from stock_dataload.dashboard import create_app

        from stock_dataload.auth import UserStore

        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        UserStore(self.settings).create_user("admin", "Admin-Pass-1", role="admin")
        app = create_app(load_settings(self.settings.config_file))
        self.state = app.extensions["state"]
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.page = self._browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.on("dialog", lambda d: d.accept())   # accept confirm() prompts
        self.sign_in("admin", "Admin-Pass-1")

    def sign_in(self, username: str, password: str) -> None:
        pg = self.page
        pg.goto(self.base + "/login")
        pg.fill("input[name=username]", username)
        pg.fill("input[name=password]", password)
        pg.get_by_role("button", name="Sign in").click()
        pg.wait_for_load_state()

    def tearDown(self):
        self.page.close()
        self.server.shutdown()
        self.thread.join(timeout=5)
        super().tearDown()

    def drop(self, name: str, folder: str = "conversion") -> Path:
        dest = getattr(self.state.settings, f"{folder}_dir") / name
        shutil.copy(SAMPLES / name, dest)
        return dest

    def flashes(self) -> list[str]:
        return [t.strip() for t in self.page.locator(".flash").all_inner_texts()]

    # ------------------------------------------------------------------
    def test_convert_all_and_load_all_by_clicking(self):
        for name in ("NASDAQ_20250123.csv", "NASDAQ_20250123.xlsx", "NASDAQ_20250123.json", "NASDAQ_20250123.xml",
                     "NASDAQ_20250123.tsv"):
            self.drop(name)
        pg = self.page
        pg.goto(self.base + "/")
        self.assertEqual(pg.title(), "Overview - dashboard")
        # the matching custom format is pre-selected for the Excel file
        row = pg.locator("tr", has_text="NASDAQ_20250123.xlsx")
        self.assertEqual(row.locator("select[name=format]").input_value(), "excel_prices")

        pg.get_by_role("button", name="Convert all").click()
        pg.wait_for_load_state()
        self.assertIn("Converted 5 file(s)", self.flashes()[0])
        self.assertEqual(pg.locator("span.badge.ok", has_text="default format").count(), 5)
        for p in self.state.settings.loading_dir.iterdir():
            self.assertEqual(p.read_text(encoding="utf-8"), ORIGINAL_TEXT, p.name)

        pg.get_by_role("button", name="Load all").click()
        pg.wait_for_load_state()
        msgs = self.flashes()
        self.assertEqual(len(msgs), 5)
        self.assertTrue(all("loaded (4092 rows, market=NASDAQ" in m for m in msgs), msgs)
        self.assertIn("20,460", pg.locator(".stat").inner_text())
        self.assertEqual(StageDatabase(self.state.settings).count_rows(), 5 * 4092)

    def test_preview_then_convert_now(self):
        self.drop("NASDAQ_20250123.json")
        pg = self.page
        pg.goto(self.base + "/")
        pg.locator("tr", has_text="NASDAQ_20250123.json").get_by_role("button", name="Preview").click()
        pg.wait_for_load_state()
        self.assertEqual(pg.locator("text=ready to convert").count(), 1)
        self.assertIn("prices.open", pg.locator(".kv").inner_text())
        self.assertEqual(pg.locator(".preview-table").nth(1).locator("tr").nth(1).inner_text().split("\t")[:2],
                         ["AACG", "20250123"])
        pg.get_by_role("button", name="Convert now").click()
        pg.wait_for_load_state()
        self.assertIn("Converted NASDAQ_20250123.json", self.flashes()[0])
        self.assertTrue((self.state.settings.loading_dir / "NASDAQ_20250123.txt").exists())

    def test_create_format_in_editor_with_run_preview_then_use_it(self):
        self.write("conversion/VENDOR_20250123.csv", "Code,Day,O,H,L,C,Qty\nAAA,2025-01-23,1,2,0.5,1.5,100\n")
        pg = self.page
        pg.goto(self.base + "/formats")
        pg.click("text=+ New format")
        pg.fill("input[name=name]", "vendor_csv")
        pg.select_option("select[name=file_type]", "csv")
        pg.fill("input[name=file_pattern]", "VENDOR_*.csv")
        pg.check("input[name=mapping_mode][value=map]")
        for col, src in {"ticker": "Code", "date": "Day", "open": "O", "high": "H", "low": "L", "close": "C", "vol": "Qty"}.items():
            pg.fill(f"input[name=map_{col}]", src)
        pg.select_option("select[name=test_file]", "VENDOR_20250123.csv")
        pg.click("button[value=test]")
        pg.wait_for_load_state()
        self.assertEqual(pg.locator(".badge.ok", has_text="OK").count(), 1)
        self.assertIn("AAA\t20250123\t1\t2\t0.5\t1.5\t100", pg.locator(".preview-table").inner_text())
        self.assertFalse(self.state.store.exists("vendor_csv"))      # preview does not save
        pg.click("button[value=save]")
        pg.wait_for_load_state()
        self.assertEqual(self.flashes(), ["Format 'vendor_csv' saved"])
        self.assertTrue(self.state.store.exists("vendor_csv"))

        pg.goto(self.base + "/")
        row = pg.locator("tr", has_text="VENDOR_20250123.csv")
        self.assertEqual(row.locator("select[name=format]").input_value(), "vendor_csv")
        row.get_by_role("button", name="Convert", exact=True).click()
        pg.wait_for_load_state()
        self.assertEqual((self.state.settings.loading_dir / "VENDOR_20250123.txt").read_text(),
                         "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\nAAA,20250123,1,2,0.5,1.5,100\n")

        # edit, clone, delete with confirm dialog
        pg.goto(self.base + "/formats")
        pg.locator("tr", has_text="vendor_csv").get_by_role("link", name="Edit").click()
        pg.fill("input[name=description]", "edited")
        pg.click("button[value=save]")
        pg.wait_for_load_state()
        self.assertEqual(self.state.store.get("vendor_csv").description, "edited")
        pg.locator("tr", has_text="vendor_csv").get_by_role("link", name="Clone").click()
        self.assertEqual(pg.input_value("input[name=name]"), "vendor_csv_copy")
        pg.click("button[value=save]")
        pg.wait_for_load_state()
        pg.locator("tr", has_text="vendor_csv_copy").get_by_role("button", name="Delete").click()
        pg.wait_for_load_state()
        self.assertFalse(self.state.store.exists("vendor_csv_copy"))
        # validation message shown in the editor
        pg.goto(self.base + "/formats/new")
        pg.fill("input[name=name]", "yahoo")
        pg.click("button[value=save]")
        self.assertIn("already exists", pg.locator(".flash.error").inner_text())

    def test_change_folders_in_settings_and_upload(self):
        pg = self.page
        pg.goto(self.base + "/settings")
        pg.fill("input[name=conversion_dir]", "same")
        pg.fill("input[name=loading_dir]", "same")
        pg.click("text=Save settings")
        self.assertIn("must be different", pg.locator(".flash.error").inner_text())
        pg.fill("input[name=conversion_dir]", "inbox")
        pg.fill("input[name=loading_dir]", "ready")
        pg.click("text=Save settings")
        pg.wait_for_load_state()
        self.assertIn("Settings saved", self.flashes()[0])
        self.assertEqual(load_settings(self.root / "config.ini").loading_dir, self.root / "ready")

        pg.goto(self.base + "/")
        self.assertIn(str(self.root / "inbox"), pg.locator(".card .folder").first.inner_text())
        upload_form = pg.locator("form[action='/upload']").first
        upload_form.locator("input[name=files]").set_input_files(str(SAMPLES / "NASDAQ_20250123.xml"))
        upload_form.get_by_role("button", name="Upload").click()
        pg.wait_for_load_state()
        self.assertTrue((self.root / "inbox" / "NASDAQ_20250123.xml").exists())
        # an .xml cannot be uploaded straight into the loading folder
        bad_form = pg.locator("form[action='/upload']").nth(1)
        bad_form.locator("input[name=files]").set_input_files(str(SAMPLES / "NASDAQ_20250123.xml"))
        bad_form.get_by_role("button", name="Upload .txt").click()
        pg.wait_for_load_state()
        self.assertIn("only default-format .txt files", pg.locator(".flash.error").inner_text())
        pg.locator("tr", has_text="NASDAQ_20250123.xml").get_by_role("button", name="Convert", exact=True).click()
        pg.wait_for_load_state()
        self.assertEqual((self.root / "ready" / "NASDAQ_20250123.txt").read_text(encoding="utf-8"), ORIGINAL_TEXT)

    def test_non_default_file_is_flagged_and_quarantined(self):
        self.drop("NASDAQ_20250123_reordered.txt", "loading")
        pg = self.page
        pg.goto(self.base + "/")
        row = pg.locator("tr", has_text="NASDAQ_20250123_reordered.txt")
        self.assertEqual(row.locator(".badge.err").inner_text(), "not loadable")
        self.assertTrue(row.get_by_role("button", name="Load", exact=True).is_disabled())
        pg.get_by_role("button", name="Load all").click()
        pg.wait_for_load_state()
        self.assertIn("run the converter first", self.flashes()[0])
        self.assertIn("Errors", pg.locator("h2", has_text="Errors").inner_text())
        self.assertTrue((self.state.settings.error_dir / "NASDAQ_20250123_reordered.txt.error.txt").exists())
        pg.goto(self.base + "/browse/error_dir")
        pg.locator("tr", has_text=".error.txt").get_by_role("button", name="delete").click()
        pg.wait_for_load_state()
        self.assertFalse((self.state.settings.error_dir / "NASDAQ_20250123_reordered.txt.error.txt").exists())


    def test_monitoring_pages_track_loads_errors_and_alerts(self):
        """A user loads a good day, a broken file and a day with a missing stock, then
        follows the Monitoring pages to see what happened."""
        register_listed(self.state.settings, "NYSE", ["AAA", "BBB"])
        loading = self.state.settings.loading_dir
        header = "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
        (loading / "NYSE_20250109.txt").write_text(
            header + "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n", encoding="utf-8")
        (loading / "BROKEN_20250109.txt").write_text("a,b\n1,2\n", encoding="utf-8")
        pg = self.page
        pg.goto(self.base + "/")
        pg.get_by_role("button", name="Load all").click()
        pg.wait_for_load_state()
        # Friday 10 Jan: BBB missing and AAA's row has high < low
        (loading / "NYSE_20250110.txt").write_text(
            header + "AAA,20250110,1,0.4,0.5,1.5,100\n", encoding="utf-8")
        pg.goto(self.base + "/")
        pg.get_by_role("button", name="Load all").click()
        pg.wait_for_load_state()
        strip = pg.locator(".monitor-strip").inner_text()
        self.assertIn("1 error(s)", strip)
        self.assertIn("2 alert(s)", strip)          # AAA (broken row) and BBB (absent)

        pg.click("header nav >> text=Monitoring")
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Monitoring - dashboard")
        self.assertIn("loader/insert_rows", pg.locator("main").inner_text())
        self.assertIn("BBB", pg.locator("main").inner_text())
        if os.environ.get("STOCK_DATALOAD_SCREENSHOTS"):   # refresh the README picture on demand
            pg.screenshot(path=str(Path(__file__).resolve().parent.parent / "docs" / "dashboard_monitoring.png"),
                          full_page=True)

        pg.click(".monitor-nav >> text=Errors")
        pg.wait_for_load_state()
        self.assertIn("FileFormatError", pg.locator("table").inner_text())
        pg.click("text=stack trace")
        self.assertIn("Traceback", pg.locator("details pre").first.inner_text())

        pg.click(".monitor-nav >> text=Alerts")
        pg.wait_for_load_state()
        rows = pg.locator("table tr").count() - 1
        self.assertEqual(rows, 2)
        self.assertIn("missing_market_day", pg.locator("table").inner_text())

        pg.click(".monitor-nav >> text=Data errors")
        pg.wait_for_load_state()
        self.assertIn("high_above_low", pg.locator("table").inner_text())
        pg.fill("input[name=file]", "NYSE_20250109.txt")
        pg.get_by_role("button", name="Filter").click()
        pg.wait_for_load_state()
        self.assertIn("No data errors match", pg.locator("main").inner_text())

        pg.click(".monitor-nav >> text=Load history")
        pg.wait_for_load_state()
        text = pg.locator("table").inner_text()
        self.assertIn("BROKEN_20250109.txt", text)
        self.assertIn("failed", text)

        pg.click(".monitor-nav >> text=Activity log")
        pg.wait_for_load_state()
        pg.select_option("select[name=level]", "ALERT")
        pg.get_by_role("button", name="Filter").click()
        pg.wait_for_load_state()
        self.assertEqual(pg.locator("span.badge", has_text="ALERT").count(), 2)


    def test_correct_a_data_error_by_clicking_through(self):
        """Data errors page -> 'Correct' -> the row is populated -> fix it -> resolved."""
        loading = self.state.settings.loading_dir
        header = "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
        (loading / "NYSE_20250110.txt").write_text(
            header + "AAA,20250110,1,0.4,0.5,1.5,100\nBBB,20250110,1,2,0.5,1.5,100\n", encoding="utf-8")
        pg = self.page
        pg.goto(self.base + "/")
        pg.get_by_role("button", name="Load all").click()
        pg.wait_for_load_state()

        pg.goto(self.base + "/monitor/data-errors")
        row = pg.locator("table tr", has_text="high_above_low")
        self.assertEqual(row.count(), 1)
        self.assertIn("open", row.inner_text())
        row.get_by_role("link", name="Correct").click()
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Data correction - dashboard")
        # the same row is populated for correction
        self.assertIn("high_above_low", pg.locator("main").inner_text())
        self.assertEqual(pg.input_value("input[name=Ticker]"), "AAA")
        self.assertEqual(pg.input_value("input[name=TradeDate]"), "20250110")
        self.assertEqual(pg.input_value("input[name=High]"), "0.4")
        self.assertEqual(pg.input_value("input[name=Low]"), "0.5")

        # an invalid correction is refused ...
        pg.fill("input[name=High]", "0.2")
        pg.get_by_role("button", name="Save correction").click()
        pg.wait_for_load_state()
        self.assertIn("smaller than low", pg.locator(".flash.error").inner_text())
        # ... a valid one is saved
        pg.fill("input[name=High]", "2")
        pg.fill("input[name=note]", "high and low were swapped")
        pg.get_by_role("button", name="Save correction").click()
        pg.wait_for_load_state()
        self.assertIn("Correction saved", self.flashes()[0])
        self.assertIn("High: '0.4' -> '2'", self.flashes()[0])
        self.assertIn("high and low were swapped", pg.locator("main").inner_text())
        if os.environ.get("STOCK_DATALOAD_SCREENSHOTS"):
            pg.screenshot(path=str(Path(__file__).resolve().parent.parent / "docs" / "dashboard_correction.png"),
                          full_page=True)
        stage = StageDatabase(self.state.settings)
        self.assertEqual(stage.find_stage_rows(ticker="AAA", trade_date="20250110")[0]["High"], "2")

        pg.click("text=Back to data errors")
        pg.wait_for_load_state()
        row = pg.locator("table tr", has_text="high_above_low")
        self.assertIn("resolved", row.inner_text())


    def test_sign_in_manage_users_and_roles(self):
        """The administrator adds an operator on the Users page; the operator signs in and
        sees operator features, but not the administration pages."""
        pg = self.page
        self.assertIn("admin", pg.locator("header .userbox").inner_text())
        pg.click("header nav >> text=Users")
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Users - dashboard")
        pg.click("text=+ Add user")
        pg.wait_for_load_state()
        pg.fill("input[name=username]", "ops")
        pg.fill("input[name=display_name]", "Olive Operator")
        pg.select_option("select[name=role]", "operator")
        pg.fill("input[name=password]", "Operator-Pass-1")
        pg.fill("input[name=confirm_password]", "Operator-Pass-1")
        pg.uncheck("input[name=must_change_password]")
        pg.get_by_role("button", name="Create user").click()
        pg.wait_for_load_state()
        self.assertIn("created with role operator", self.flashes()[0])
        self.assertIn("Olive Operator", pg.locator("table").inner_text())
        if os.environ.get("STOCK_DATALOAD_SCREENSHOTS"):
            pg.screenshot(path=str(Path(__file__).resolve().parent.parent / "docs" / "dashboard_users.png"),
                          full_page=True)

        pg.get_by_role("button", name="Sign out").click()
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Sign in - dashboard")
        # wrong password is refused
        pg.fill("input[name=username]", "ops")
        pg.fill("input[name=password]", "wrong")
        pg.get_by_role("button", name="Sign in").click()
        pg.wait_for_load_state()
        self.assertIn("wrong password", pg.locator(".flash.error").inner_text())
        self.sign_in("ops", "Operator-Pass-1")
        self.assertEqual(pg.title(), "Overview - dashboard")
        nav = pg.locator("header nav").inner_text()
        self.assertIn("Data correction", nav)
        self.assertNotIn("Users", nav)
        self.assertNotIn("Settings", nav)
        pg.goto(self.base + "/users")
        self.assertIn("Not allowed", pg.locator("main").inner_text())


    def test_bulk_load_a_folder_and_promote_by_clicking(self):
        """Overview: bulk load a folder -> Active tables: run the promotion -> browse the weekly table."""
        folder = self.root / "deliveries"
        folder.mkdir()
        header = "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
        for day, close in (("20250106", "1.5"), ("20250107", "1.6"), ("20250108", "1.7")):
            (folder / f"NASDAQ_{day}.txt").write_text(header + f"AAA,{day},1,2,0.5,{close},100\n", encoding="utf-8")
        (folder / "NASDAQ_20250105.txt").write_text(header + "AAA,20250105,1,2,0.5,1.4,100\n", encoding="utf-8")  # Sunday
        pg = self.page
        pg.goto(self.base + "/")
        pg.fill("input[name=folder]", str(folder))
        pg.get_by_role("button", name="Load folder").click()
        pg.wait_for_load_state()
        msgs = self.flashes()
        self.assertIn("3 of 4 file(s) loaded (3 rows), 1 skipped (no trading day)", msgs[0])
        self.assertTrue(any("NASDAQ_20250105.txt: skipped - NASDAQ was closed on 2025-01-05 (weekend (Sunday))" in m
                            for m in msgs), msgs)
        self.assertEqual(len(list(folder.iterdir())), 4, "files stay in place")
        self.assertIn("3", pg.locator(".stat").first.inner_text())

        pg.click("header nav >> text=Active tables")
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Active tables - dashboard")
        self.assertIn("never promoted", pg.locator("main").inner_text())
        pg.select_option("select[name=what]", "all")
        pg.get_by_role("button", name="Run promotion").click()
        pg.wait_for_load_state()
        msgs = self.flashes()
        self.assertTrue(any("promote_daily: completed (full) - 3 rows read: 3 inserted" in m for m in msgs), msgs)
        self.assertTrue(any("promote_weekly: completed" in m and "1 inserted" in m for m in msgs), msgs)
        self.assertTrue(any("promote_monthly: completed" in m and "1 inserted" in m for m in msgs), msgs)
        cards = pg.locator(".stat-card").all_inner_texts()
        self.assertIn("3", cards[0])
        self.assertEqual(pg.locator("table tr", has_text="promote_daily").count(), 1)
        if os.environ.get("STOCK_DATALOAD_SCREENSHOTS"):
            pg.screenshot(path=str(Path(__file__).resolve().parent.parent / "docs" / "dashboard_promote.png"),
                          full_page=True)

        pg.click(".stat-card >> text=weekly rows")
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Weekly table - dashboard")
        text = pg.locator("table").inner_text()
        self.assertIn("2025-01-06", text)        # WeekStart (Monday)
        self.assertIn("2025-01-12", text)        # WeekEnd (Sunday)
        self.assertIn("1.7", text)               # close of the last day
        self.assertIn("300", text)               # volume of the week
        pg.fill("input[name=ticker]", "ZZZ")
        pg.get_by_role("button", name="Filter").click()
        pg.wait_for_load_state()
        self.assertIn("No rows match", pg.locator("main").inner_text())

        # the skipped Sunday delivery is in the load history and the calendar explains why
        pg.goto(self.base + "/monitor/history")
        row = pg.locator("table tr", has_text="NASDAQ_20250105.txt")
        self.assertIn("skipped", row.inner_text())
        pg.click("header nav >> text=Trading calendar")
        pg.wait_for_load_state()
        self.assertEqual(pg.title(), "Trading calendar - dashboard")
        self.assertIn("NASDAQ", pg.locator("main").inner_text())
        pg.select_option("form[method=get] select[name=market]", "NASDAQ")
        pg.fill("form[method=get] input[name=year]", "2025")
        pg.check("form[method=get] input[name=all]")
        pg.get_by_role("button", name="Show").click()
        pg.wait_for_load_state()
        self.assertIn("weekend (Sunday)", pg.locator("table tr", has_text="2025-01-05").inner_text())
        if os.environ.get("STOCK_DATALOAD_SCREENSHOTS"):
            pg.screenshot(path=str(Path(__file__).resolve().parent.parent / "docs" / "dashboard_calendar.png"),
                          full_page=True)


if __name__ == "__main__":
    unittest.main()
