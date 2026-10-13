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

from .helpers import ORIGINAL, SAMPLES, TempProjectTestCase

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

        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        app = create_app(load_settings(self.settings.config_file))
        self.state = app.extensions["state"]
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.page = self._browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.on("dialog", lambda d: d.accept())   # accept confirm() prompts

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


if __name__ == "__main__":
    unittest.main()
