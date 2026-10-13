"""
Dashboard tests (Flask test client - no network needed).

Validates that business users can, through the web UI:
  * configure custom file formats (create / test / edit / clone / delete)
  * configure the conversion folder and the loading folder (and the others)
  * upload / convert csv, text, excel, json, xml files into the loading folder
  * load the loading folder into the staging table
and that every file placed into the loading folder is a default-format text file.
"""

import json
import shutil
import unittest
from io import BytesIO
from pathlib import Path

from stock_dataload.config import load_settings, save_settings
from stock_dataload.dashboard import create_app
from stock_dataload.db import StageDatabase
from stock_dataload.file_formats import FormatStore

from .helpers import DEFAULT_HEADER, ORIGINAL, SAMPLES, TempProjectTestCase

ORIGINAL_TEXT = ORIGINAL.read_text(encoding="utf-8")


class DashboardTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        # the dashboard persists settings -> give it a real config.ini in the temp project
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        self.settings = load_settings(self.settings.config_file)
        self.app = create_app(self.settings)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.state = self.app.extensions["state"]

    def drop(self, name: str, folder: str = "conversion") -> Path:
        dest = getattr(self.state.settings, f"{folder}_dir") / name
        shutil.copy(SAMPLES / name, dest)
        return dest

    def flashes(self, response) -> str:
        """Follow the redirect and return the page text (flash messages included)."""
        page = self.client.get(response.headers.get("Location", "/"))
        return page.get_data(as_text=True)


class PagesRenderTests(DashboardTestCase):
    def test_all_pages_render(self):
        for url in ("/", "/formats", "/formats/new", "/formats/excel_prices/edit", "/formats/new?clone=yahoo",
                    "/settings", "/browse/archive_dir", "/api/status"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_overview_shows_folders_and_default_format(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(str(self.state.settings.conversion_dir), html)
        self.assertIn(str(self.state.settings.loading_dir), html)
        self.assertIn("&lt;ticker&gt;,&lt;date&gt;,&lt;open&gt;,&lt;high&gt;,&lt;low&gt;,&lt;close&gt;,&lt;vol&gt;", html)

    def test_unknown_format_and_folder_404(self):
        self.assertEqual(self.client.get("/formats/nope/edit").status_code, 404)
        self.assertEqual(self.client.get("/browse/etc").status_code, 404)
        self.assertEqual(self.client.get("/api/formats/nope").status_code, 404)

    def test_path_traversal_is_rejected(self):
        self.assertEqual(self.client.get("/preview?file=../config.ini").status_code, 400)
        self.assertEqual(self.client.get("/files/conversion_dir/..%2Fconfig.ini").status_code, 400)
        self.assertEqual(self.client.post("/convert", data={"file": "../../etc/passwd"}).status_code, 400)
        self.assertEqual(self.client.get("/preview?file=nothere.csv").status_code, 404)


class ConvertAndLoadThroughDashboardTests(DashboardTestCase):
    def test_convert_each_file_type_then_load(self):
        files = ["NASDAQ_20250123.csv", "NASDAQ_20250123.tsv", "NASDAQ_20250123.xlsx",
                 "NASDAQ_20250123.json", "NASDAQ_20250123.xml"]
        for name in files:
            self.drop(name)
        html = self.client.get("/").get_data(as_text=True)
        for name in files:
            self.assertIn(name, html)

        # convert one by one through the per-file form (format auto / suggested)
        for name in files:
            r = self.client.post("/convert", data={"file": name, "format": "", "on_conflict": "rename",
                                                   "move_source": "on"})
            self.assertEqual(r.status_code, 302)
            self.assertIn("Converted " + name, self.flashes(r))
        outputs = sorted(p.name for p in self.state.settings.loading_dir.iterdir())
        self.assertEqual(outputs, ["NASDAQ_20250123.txt", "NASDAQ_20250123_json.txt", "NASDAQ_20250123_tsv.txt",
                                   "NASDAQ_20250123_xlsx.txt", "NASDAQ_20250123_xml.txt"])
        for p in self.state.settings.loading_dir.iterdir():
            self.assertEqual(p.suffix, ".txt")
            self.assertEqual(p.read_text(encoding="utf-8"), ORIGINAL_TEXT, p.name)   # default format, identical data
        self.assertEqual(self.state.converter.pending_sources(), [])                 # sources moved away

        # overview marks them loadable
        html = self.client.get("/").get_data(as_text=True)
        self.assertEqual(html.count("default format</span>"), 5)

        # load everything
        r = self.client.post("/load", data={"market": ""})
        page = self.flashes(r)
        self.assertEqual(page.count("loaded (4092 rows"), 5)
        status = self.client.get("/api/status").get_json()
        self.assertEqual(sorted(l["source_file"] for l in status["loads"]), outputs)
        self.assertEqual(sum(l["rows"] for l in status["loads"]), 5 * 4092)
        self.assertEqual(status["loading_files"], [])
        self.assertEqual(StageDatabase(self.state.settings).count_rows(), 5 * 4092)

    def test_convert_all_button(self):
        for name in ("NASDAQ_20250123_broker.csv", "NASDAQ_20250123_noheader.txt", "NASDAQ_20250123.xml"):
            self.drop(name)
        r = self.client.post("/convert-all", data={"on_conflict": "rename", "move_source": "on"})
        page = self.flashes(r)
        self.assertIn("Converted 3 file(s)", page)
        self.assertEqual(len(list(self.state.settings.loading_dir.glob("*.txt"))), 3)

    def test_failed_conversion_is_reported_and_file_stays(self):
        bad = self.write("conversion/mystery.csv", "a,b,c\n1,2,3\n")
        r = self.client.post("/convert", data={"file": "mystery.csv"})
        page = self.flashes(r)
        self.assertIn("Conversion of mystery.csv failed", page)
        self.assertTrue(bad.exists())
        r = self.client.post("/convert-all", data={})
        self.assertTrue(bad.with_name("mystery.csv.error.txt").exists())
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("could not find column(s)", html)

    def test_non_default_file_in_loading_folder_is_flagged_and_not_loaded(self):
        self.drop("NASDAQ_20250123_reordered.txt", "loading")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("not loadable", html)
        r = self.client.post("/load", data={})
        self.assertIn("failed", self.flashes(r))
        self.assertTrue((self.state.settings.error_dir / "NASDAQ_20250123_reordered.txt").exists())

    def test_load_single_file_with_market_and_truncate(self):
        self.write("loading/A_20250101.txt", DEFAULT_HEADER + "AAA,20250101,1,2,0.5,1.5,100\n")
        self.write("loading/B_20250101.txt", DEFAULT_HEADER + "BBB,20250101,1,2,0.5,1.5,100\n")
        self.client.post("/load", data={"file": "A_20250101.txt", "market": "TEST"})
        db = StageDatabase(self.state.settings)
        with db.connection() as conn:
            rows = db.fetch_all(conn)
        self.assertEqual([(r[0], r[1]) for r in rows], [("TEST", "AAA")])
        self.client.post("/load", data={"file": "B_20250101.txt", "truncate": "on"})
        with db.connection() as conn:
            rows = db.fetch_all(conn)
        self.assertEqual([r[1] for r in rows], ["BBB"])

    def test_preview_page_and_json(self):
        self.drop("NASDAQ_20250123.json")
        r = self.client.get("/preview?file=NASDAQ_20250123.json")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("ready to convert", html)
        self.assertIn("AACG", html)
        data = self.client.get("/preview?file=NASDAQ_20250123.json&json=1").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["file_type"], "json")
        self.assertEqual(data["total_rows"], 4092)
        self.assertEqual(data["converted_rows"][0], ["AACG", "20250123", "0.9", "0.9229", "0.89", "0.9128", "2800"])
        self.assertEqual(data["mapping"]["ticker"], 1)

    def test_upload_to_conversion_and_loading_folders(self):
        payload = {"target": "conversion_dir",
                   "files": (BytesIO((SAMPLES / "NASDAQ_20250123.xlsx").read_bytes()), "NASDAQ_20250123.xlsx")}
        r = self.client.post("/upload", data=payload, content_type="multipart/form-data")
        self.assertIn("Uploaded NASDAQ_20250123.xlsx", self.flashes(r))
        self.assertTrue((self.state.settings.conversion_dir / "NASDAQ_20250123.xlsx").exists())
        # only .txt files may go straight into the loading folder
        payload = {"target": "loading_dir", "files": (BytesIO(b"x"), "NASDAQ_20250123.csv")}
        r = self.client.post("/upload", data=payload, content_type="multipart/form-data")
        self.assertIn("only default-format .txt files", self.flashes(r))
        self.assertFalse((self.state.settings.loading_dir / "NASDAQ_20250123.csv").exists())
        payload = {"target": "loading_dir", "files": (BytesIO(ORIGINAL_TEXT.encode()), "NASDAQ_20250123.txt")}
        r = self.client.post("/upload", data=payload, content_type="multipart/form-data")
        self.assertTrue((self.state.settings.loading_dir / "NASDAQ_20250123.txt").exists())

    def test_download_and_delete_file(self):
        self.drop("NASDAQ_20250123.csv")
        r = self.client.get("/files/conversion_dir/NASDAQ_20250123.csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith("Symbol,Open"))
        r = self.client.post("/files/conversion_dir/NASDAQ_20250123.csv/delete")
        self.assertEqual(r.status_code, 302)
        self.assertFalse((self.state.settings.conversion_dir / "NASDAQ_20250123.csv").exists())


class ConfigureFormatsThroughDashboardTests(DashboardTestCase):
    MAP_FORM = {"name": "my_csv", "description": "NASDAQ csv export", "file_type": "csv",
                "file_pattern": "NASDAQ_*.csv", "has_header": "on", "mapping_mode": "map",
                "map_ticker": "Symbol", "map_date": "#7", "map_open": "Open", "map_high": "High",
                "map_low": "Low", "map_close": "Close", "map_vol": "Volume"}

    def test_create_test_save_edit_clone_delete(self):
        self.drop("NASDAQ_20250123.csv")
        # "Run preview" with the unsaved form
        r = self.client.post("/formats/new", data={**self.MAP_FORM, "action": "test", "test_file": "NASDAQ_20250123.csv"})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('badge ok">OK', html)
        self.assertIn("AACG", html)
        self.assertFalse(self.state.store.exists("my_csv"))           # test does not save
        # save
        r = self.client.post("/formats/new", data={**self.MAP_FORM, "action": "save"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.state.store.exists("my_csv"))
        on_disk = json.loads(self.state.settings.formats_file.read_text())
        saved = [f for f in on_disk["formats"] if f["name"] == "my_csv"][0]
        self.assertEqual(saved["column_map"]["date"], "#7")
        self.assertEqual(saved["file_pattern"], "NASDAQ_*.csv")
        # the new format is now suggested for matching files on the overview and used by convert
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('value="my_csv" selected', html)
        r = self.client.post("/convert", data={"file": "NASDAQ_20250123.csv", "format": "my_csv"})
        self.assertIn("Converted NASDAQ_20250123.csv", self.flashes(r))
        self.assertEqual((self.state.settings.loading_dir / "NASDAQ_20250123.txt").read_text(), ORIGINAL_TEXT)
        # edit (rename + change description)
        r = self.client.post("/formats/my_csv/edit", data={**self.MAP_FORM, "name": "my_csv2", "description": "renamed",
                                                           "action": "save"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.state.store.exists("my_csv"))
        self.assertEqual(self.state.store.get("my_csv2").description, "renamed")
        self.assertEqual(self.client.get("/api/formats/my_csv2").get_json()["description"], "renamed")
        # clone pre-fills the form
        html = self.client.get("/formats/new?clone=my_csv2").get_data(as_text=True)
        self.assertIn('value="my_csv2_copy"', html)
        # delete
        r = self.client.post("/formats/my_csv2/delete")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.state.store.exists("my_csv2"))

    def test_validation_errors_are_shown(self):
        r = self.client.post("/formats/new", data={"name": "", "file_type": "csv", "mapping_mode": "auto", "action": "save"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("format name is required", r.get_data(as_text=True))
        r = self.client.post("/formats/new", data={"name": "yahoo", "file_type": "csv", "has_header": "on",
                                                   "mapping_mode": "auto", "action": "save"})
        self.assertIn("already exists", r.get_data(as_text=True))
        r = self.client.post("/formats/new", data={"name": "nh", "file_type": "text", "mapping_mode": "auto", "action": "save"})
        self.assertIn("without header", r.get_data(as_text=True))
        r = self.client.post("/formats/new", data={**self.MAP_FORM, "action": "test"})   # no test file chosen
        self.assertIn("choose a file", r.get_data(as_text=True))

    def test_sequence_mode_format_for_headerless_text(self):
        self.drop("NASDAQ_20250123_noheader.txt")
        form = {"name": "pipes", "file_type": "text", "delimiter": "pipe", "mapping_mode": "sequence",
                "columns": "date,ticker,open,high,low,close,vol", "file_pattern": "*_noheader.txt", "action": "save"}
        self.client.post("/formats/new", data=form)
        fmt = self.state.store.get("pipes")
        self.assertEqual(fmt.delimiter, "|")
        self.assertFalse(fmt.has_header)
        r = self.client.post("/convert", data={"file": "NASDAQ_20250123_noheader.txt", "format": "pipes"})
        self.assertIn("Converted", self.flashes(r))
        self.assertEqual((self.state.settings.loading_dir / "NASDAQ_20250123_noheader.txt").read_text(), ORIGINAL_TEXT)


class ConfigureFoldersThroughDashboardTests(DashboardTestCase):
    def settings_form(self, **overrides):
        s = self.state.settings
        form = {
            "conversion_dir": str(s.conversion_dir), "converted_dir": str(s.converted_dir),
            "loading_dir": str(s.loading_dir), "archive_dir": str(s.archive_dir), "error_dir": str(s.error_dir),
            "work_dir": str(s.work_dir), "log_dir": str(s.log_dir), "formats_file": str(s.formats_file),
            "sqlite_path": str(s.sqlite_path), "stage_table": s.stage_table, "batch_size": str(s.batch_size),
            "replace_existing_file_rows": "on", "file_patterns": ";".join(s.file_patterns),
            "market_from_filename_regex": s.market_from_filename_regex, "default_market": s.default_market,
            "encoding": s.encoding,
        }
        form.update(overrides)
        return form

    def test_change_conversion_and_loading_folder(self):
        r = self.client.post("/settings", data=self.settings_form(conversion_dir="inbox", loading_dir="ready",
                                                                  default_market="NYSE"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("Settings saved", self.flashes(r))
        # in memory
        s = self.state.settings
        self.assertEqual(s.conversion_dir, self.root / "inbox")
        self.assertEqual(s.loading_dir, self.root / "ready")
        self.assertEqual(s.default_market, "NYSE")
        self.assertTrue((self.root / "inbox").is_dir() and (self.root / "ready").is_dir())
        # on disk (config.ini), relative paths kept relative
        ini = (self.root / "config.ini").read_text()
        self.assertIn("conversion_dir = inbox", ini)
        self.assertIn("loading_dir = ready", ini)
        self.assertIn("default_market = NYSE", ini)
        self.assertEqual(load_settings(self.root / "config.ini").loading_dir, self.root / "ready")
        # the whole pipeline now uses the new folders
        shutil.copy(SAMPLES / "NASDAQ_20250123.json", self.root / "inbox" / "NASDAQ_20250123.json")
        self.client.post("/convert", data={"file": "NASDAQ_20250123.json"})
        self.assertEqual((self.root / "ready" / "NASDAQ_20250123.txt").read_text(), ORIGINAL_TEXT)
        page = self.flashes(self.client.post("/load", data={}))
        self.assertIn("loaded (4092 rows, market=NASDAQ", page)
        self.assertTrue((self.root / "archive" / "NASDAQ_20250123.txt").exists())

    def test_invalid_settings_are_rejected(self):
        r = self.client.post("/settings", data=self.settings_form(loading_dir=""))
        self.assertEqual(r.status_code, 400)
        self.assertIn("must not be empty", r.get_data(as_text=True))
        r = self.client.post("/settings", data=self.settings_form(loading_dir="same", conversion_dir="same"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("must be different", r.get_data(as_text=True))
        r = self.client.post("/settings", data=self.settings_form(batch_size="0"))
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/settings", data=self.settings_form(market_from_filename_regex="("))
        self.assertEqual(r.status_code, 400)
        # nothing changed
        self.assertEqual(self.state.settings.loading_dir, self.root / "loading")

    def test_change_formats_file_location(self):
        r = self.client.post("/settings", data=self.settings_form(formats_file="cfg/my_formats.json"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.state.settings.formats_file, self.root / "cfg" / "my_formats.json")
        self.client.post("/formats/new", data={"name": "n1", "file_type": "json", "mapping_mode": "auto", "action": "save"})
        self.assertIn("n1", FormatStore(self.root / "cfg" / "my_formats.json").names())


if __name__ == "__main__":
    unittest.main()
