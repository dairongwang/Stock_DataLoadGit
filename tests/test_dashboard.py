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
import unittest.mock as mock
from io import BytesIO
from pathlib import Path

from stock_dataload.auth import UserStore
from stock_dataload.config import load_settings, save_settings
from stock_dataload.dashboard import create_app
from stock_dataload.db import StageDatabase
from stock_dataload.file_formats import FormatStore

from .helpers import DEFAULT_HEADER, ORIGINAL, SAMPLES, TempProjectTestCase, register_listed

ORIGINAL_TEXT = ORIGINAL.read_text(encoding="utf-8")


ADMIN = ("admin", "Admin-Pass-1")
OPERATOR = ("ops", "Operator-Pass-1")
VIEWER = ("viewer", "Viewer-Pass-1")


class DashboardTestCase(TempProjectTestCase):
    """Every test gets a dashboard with three accounts and is signed in as the administrator."""

    def setUp(self):
        super().setUp()
        # the dashboard persists settings -> give it a real config.ini in the temp project
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        self.settings = load_settings(self.settings.config_file)
        self.users = UserStore(self.settings)
        self.users.create_user(*ADMIN, role="admin")
        self.users.create_user(*OPERATOR, role="operator")
        self.users.create_user(*VIEWER, role="viewer")
        self.app = create_app(self.settings)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.state = self.app.extensions["state"]
        self.login(*ADMIN)

    def login(self, username, password):
        r = self.client.post("/login", data={"username": username, "password": password})
        self.assertEqual(r.status_code, 302, "login failed")
        return r

    def logout(self):
        return self.client.post("/logout")

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
        self.write("loading/A_20250107.txt", DEFAULT_HEADER + "AAA,20250107,1,2,0.5,1.5,100\n")
        self.write("loading/B_20250107.txt", DEFAULT_HEADER + "BBB,20250107,1,2,0.5,1.5,100\n")
        self.client.post("/load", data={"file": "A_20250107.txt", "market": "TEST"})
        db = StageDatabase(self.state.settings)
        with db.connection() as conn:
            rows = db.fetch_all(conn)
        self.assertEqual([(r[0], r[1]) for r in rows], [("TEST", "AAA")])
        self.client.post("/load", data={"file": "B_20250107.txt", "truncate": "on"})
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
        r.close()      # the test client keeps the downloaded file open until the response is closed (Windows!)
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
            "file_patterns": ";".join(s.file_patterns),
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




class ReloadThroughDashboardTests(DashboardTestCase):
    def test_reloading_same_file_replaces_previous_rows(self):
        (self.state.settings.loading_dir / "NASDAQ_20250123.txt").write_text(
            ORIGINAL_TEXT, encoding="utf-8", newline="")
        r = self.client.post("/load", data={"file": "NASDAQ_20250123.txt"})
        self.assertIn("loaded (4092 rows", self.flashes(r))
        # the very same file dropped and loaded again -> previous rows replaced, still one copy
        (self.state.settings.loading_dir / "NASDAQ_20250123.txt").write_text(
            ORIGINAL_TEXT, encoding="utf-8", newline="")
        r = self.client.post("/load", data={"file": "NASDAQ_20250123.txt"})
        html = self.flashes(r)
        self.assertIn("4092 rows of the previous load replaced", html)
        self.assertEqual(self.state.db.count_rows(), 4092)


class MonitoringPagesTests(DashboardTestCase):
    """Business users can track the activity log, errors, data errors and load history."""

    def load_file(self, name, text):
        (self.state.settings.loading_dir / name).write_text(text, encoding="utf-8", newline="")
        return self.client.post("/load", data={"file": name})

    def test_monitoring_pages_render_empty(self):
        for url in ("/monitor", "/monitor/logs", "/monitor/errors", "/monitor/data-errors",
                    "/monitor/data-errors?alerts=1", "/monitor/history", "/api/monitor"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = self.client.get("/monitor").get_data(as_text=True)
        self.assertIn("No operational errors recorded", html)
        self.assertIn("No alerts", html)

    def test_overview_shows_monitoring_strip(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Operations monitoring", html)
        self.assertIn("/monitor/errors", html)
        self.assertIn("/monitor/data-errors?alerts=1", html)

    def test_activity_log_page_and_filters(self):
        self.load_file("NASDAQ_20250123.txt", ORIGINAL_TEXT)
        html = self.client.get("/monitor/logs").get_data(as_text=True)
        self.assertIn("file_loaded", html)
        self.assertIn("load_started", html)
        self.assertIn("NASDAQ_20250123.txt", html)
        html = self.client.get("/monitor/logs?level=ERROR").get_data(as_text=True)
        self.assertNotIn("file_loaded", html)
        self.assertIn("No log entries match", html)
        html = self.client.get("/monitor/logs?component=loader&limit=2").get_data(as_text=True)
        self.assertIn("run_complete", html)
        self.assertNotIn("load_started", html)         # limited to the 2 newest entries

    def test_error_page_shows_failed_step_and_stacktrace(self):
        self.load_file("BAD_20250107.txt", "a,b\n1,2\n")
        html = self.client.get("/monitor/errors").get_data(as_text=True)
        self.assertIn("loader/insert_rows", html)
        self.assertIn("FileFormatError", html)
        self.assertIn("not the default column sequence", html)
        self.assertIn("stack trace", html)
        self.assertIn("BAD_20250107.txt", html)
        html = self.client.get("/monitor/errors?component=converter").get_data(as_text=True)
        self.assertIn("No operational errors recorded", html)
        # the summary page and overview strip reflect it too
        self.assertIn("loader/insert_rows", self.client.get("/monitor").get_data(as_text=True))
        self.assertIn("1 error(s)", self.client.get("/").get_data(as_text=True))

    def test_data_error_page_with_alerts(self):
        register_listed(self.state.settings, "NYSE", ["AAA", "BBB"])
        self.load_file("NYSE_20250109.txt", DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\n"
                                                            "BBB,20250109,1,2,0.5,1.5,100\n")
        self.load_file("NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        html = self.client.get("/monitor/data-errors").get_data(as_text=True)
        self.assertIn("high_above_low", html)
        self.assertIn("missing_market_day", html)
        self.assertIn("ALERT", html)
        html = self.client.get("/monitor/data-errors?alerts=1").get_data(as_text=True)
        self.assertIn("missing_market_day", html)
        self.assertNotIn("high_above_low", html)
        html = self.client.get("/monitor/data-errors?file=NYSE_20250109.txt").get_data(as_text=True)
        self.assertIn("No data errors match", html)
        summary = self.client.get("/monitor").get_data(as_text=True)
        self.assertIn("BBB", summary)
        self.assertIn("2 alert(s)", self.client.get("/").get_data(as_text=True))

    def test_history_page_tracks_reloads(self):
        self.load_file("NASDAQ_20250123.txt", ORIGINAL_TEXT)
        self.load_file("NASDAQ_20250123.txt", ORIGINAL_TEXT)
        html = self.client.get("/monitor/history").get_data(as_text=True)
        self.assertIn("replaced", html)
        self.assertIn("4,092", html)
        html = self.client.get("/monitor/history?file=other.txt").get_data(as_text=True)
        self.assertIn("No load attempts match", html)

    def test_json_api_endpoints(self):
        self.load_file("BAD_20250107.txt", "a,b\n1,2\n")
        self.load_file("NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        logs = self.client.get("/api/logs?component=loader").get_json()
        self.assertEqual(logs["kind"], "logs")
        self.assertTrue(all(r["component"] == "loader" for r in logs["records"]))
        errors = self.client.get("/api/errors").get_json()
        self.assertEqual(errors["count"], 1)
        self.assertEqual(errors["records"][0]["operation"], "insert_rows")
        data = self.client.get("/api/data-errors?limit=1").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["records"][0]["rule"], "high_above_low")
        history = self.client.get("/api/history").get_json()
        self.assertEqual({r["status"] for r in history["records"]}, {"failed", "loaded"})
        counts = self.client.get("/api/monitor").get_json()
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(counts["failed_loads"], 1)
        self.assertEqual(counts["data_errors"], 1)

    def test_bad_limit_falls_back_to_default(self):
        r = self.client.get("/monitor/logs?limit=abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/logs?limit=abc").get_json()["kind"], "logs")

    def test_monitoring_survives_unusable_database(self):
        self.state.settings.sqlite_path = self.root          # a directory -> database unusable
        self.state.reload(self.state.settings)
        # the signed-in user can no longer be verified -> back to the sign-in page, no crash
        for url in ("/monitor", "/monitor/logs", "/monitor/errors", "/monitor/data-errors", "/monitor/history"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/api/logs").status_code, 401)
        r = self.client.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]})
        self.assertEqual(r.status_code, 500)                 # reported, not crashed
        self.assertIn("database problem", r.get_data(as_text=True))


class DataCorrectionPageTests(DashboardTestCase):
    """Business users correct staging rows; data errors link to the correction page."""

    def load_file(self, name, text):
        (self.state.settings.loading_dir / name).write_text(text, encoding="utf-8", newline="")
        return self.client.post("/load", data={"file": name})

    def load_bad_high_low(self):
        self.load_file("NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n"
                                                            "BBB,20250110,1,2,0.5,1.5,100\n")
        error = self.state.data_errors.fetch()[0]
        row = self.state.db.find_stage_rows(ticker="AAA", trade_date="20250110")[0]
        return error, row

    def test_search_page_renders(self):
        html = self.client.get("/correct").get_data(as_text=True)
        self.assertIn("Find the row to correct", html)
        self.assertIn("No corrections yet", html)
        self.assertIn("Data correction", self.client.get("/").get_data(as_text=True))   # nav link

    def test_data_error_row_links_to_correction_page(self):
        error, row = self.load_bad_high_low()
        html = self.client.get("/monitor/data-errors").get_data(as_text=True)
        self.assertIn(f"/correct?data_error_id={error.data_error_id}", html)
        self.assertIn(">open<", html)

    def test_correction_page_prefilled_from_data_error(self):
        error, row = self.load_bad_high_low()
        html = self.client.get(f"/correct?data_error_id={error.data_error_id}").get_data(as_text=True)
        self.assertIn(f"Data error #{error.data_error_id}", html)
        self.assertIn("high_above_low", html)
        self.assertIn(f"Correct staging row <span class=\"mono\">#{row['StageId']}</span>", html)
        self.assertIn('name="High" value="0.4"', html)
        self.assertIn('name="Low" value="0.5"', html)
        self.assertIn('name="Ticker" value="AAA"', html)
        self.assertIn(f'name="data_error_id" value="{error.data_error_id}"', html)
        self.assertIn("Open data errors for this row", html)

    def test_save_correction_from_data_error(self):
        error, row = self.load_bad_high_low()
        form = {"action": "update", "stage_id": row["StageId"], "data_error_id": error.data_error_id,
                "Market": "NYSE", "Ticker": "AAA", "TradeDate": "20250110", "Open": "1", "High": "2",
                "Low": "0.5", "Close": "1.5", "Vol": "100", "note": "swapped"}
        r = self.client.post("/correct", data=form)
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"stage_id={row['StageId']}", r.headers["Location"])
        html = self.flashes(r)
        self.assertIn("Correction saved", html)
        self.assertIn("High: &#39;0.4&#39; -&gt; &#39;2&#39;", html)
        self.assertIn("Corrections of this row", html)
        self.assertEqual(self.state.db.get_stage_row(row["StageId"])["High"], "2")
        self.assertEqual(self.state.data_errors.get(error.data_error_id).resolved, 1)
        # the data errors page now shows it resolved, open-only hides it
        html = self.client.get("/monitor/data-errors").get_data(as_text=True)
        self.assertIn("resolved", html)
        html = self.client.get("/monitor/data-errors?open=1").get_data(as_text=True)
        self.assertIn("No data errors match", html)
        self.assertIn("0 data error(s)", self.client.get("/").get_data(as_text=True))
        self.assertIn("1 resolved", self.client.get("/monitor").get_data(as_text=True))

    def test_invalid_correction_is_rejected_with_highlight(self):
        error, row = self.load_bad_high_low()
        form = {"action": "update", "stage_id": row["StageId"], "Market": "NYSE", "Ticker": "AAA",
                "TradeDate": "20250110", "Open": "1", "High": "0.3", "Low": "0.5", "Close": "abc", "Vol": "100"}
        r = self.client.post("/correct", data=form)
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn("please fix the highlighted values", html)
        self.assertIn("smaller than low", html)
        self.assertIn('name="Close" value="abc" class="bad"', html)
        self.assertEqual(self.state.db.get_stage_row(row["StageId"])["High"], "0.4")  # untouched

    def test_unchanged_correction(self):
        error, row = self.load_bad_high_low()
        form = dict({c: row[c] for c in ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Vol")},
                    action="update", stage_id=row["StageId"])
        form["High"] = "2"   # make it valid but then submit the same twice
        self.client.post("/correct", data=form)
        r = self.client.post("/correct", data=form)
        self.assertIn("nothing changed", self.flashes(r))

    def test_find_by_stage_id_and_by_criteria(self):
        error, row = self.load_bad_high_low()
        html = self.client.get(f"/correct?stage_id={row['StageId']}").get_data(as_text=True)
        self.assertIn('name="Ticker" value="AAA"', html)
        html = self.client.get("/correct?ticker=BBB&trade_date=20250110").get_data(as_text=True)
        self.assertIn('name="Ticker" value="BBB"', html)
        # several matches -> choose
        html = self.client.get("/correct?source_file=NYSE_20250110.txt").get_data(as_text=True)
        self.assertIn("2 rows match", html)
        self.assertIn(f"stage_id={row['StageId']}", html)
        # no match -> offer to add the row
        html = self.client.get("/correct?ticker=ZZZ&trade_date=20250110&market=NYSE").get_data(as_text=True)
        self.assertIn("Add the missing row", html)
        self.assertIn('name="Ticker" value="ZZZ"', html)
        self.assertIn("no staging row matches", html)

    def test_not_found_cases(self):
        html = self.client.get("/correct?stage_id=424242").get_data(as_text=True)
        self.assertIn("staging row 424242 not found", html)
        html = self.client.get("/correct?data_error_id=424242").get_data(as_text=True)
        self.assertIn("data error #424242 not found", html)
        html = self.client.get("/correct?stage_id=abc").get_data(as_text=True)
        self.assertIn("could not look up the row", html)
        r = self.client.post("/correct", data={"action": "update", "stage_id": "424242", "Ticker": "A",
                                               "TradeDate": "20250110"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("staging row not found", self.flashes(r))

    def test_add_missing_row_from_alert(self):
        register_listed(self.state.settings, "NYSE", ["AAA", "BBB"])
        self.load_file("NYSE_20250109.txt", DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\n"
                                                            "BBB,20250109,1,2,0.5,1.5,100\n")
        self.load_file("NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        alert = self.state.data_errors.fetch(alerts_only=True)[0]
        html = self.client.get(f"/correct?data_error_id={alert.data_error_id}").get_data(as_text=True)
        self.assertIn("Add the missing row", html)
        self.assertIn('name="Ticker" value="BBB"', html)
        self.assertIn('name="TradeDate" value="20250110"', html)
        self.assertIn('name="SourceFile" value="NYSE_20250110.txt"', html)
        form = {"action": "insert", "data_error_id": alert.data_error_id, "SourceFile": "NYSE_20250110.txt",
                "Market": "NYSE", "Ticker": "BBB", "TradeDate": "20250110", "Open": "1", "High": "2",
                "Low": "0.5", "Close": "1.5", "Vol": "100", "note": "late delivery"}
        r = self.client.post("/correct", data=form)
        self.assertEqual(r.status_code, 302)
        self.assertIn("inserted", self.flashes(r))
        rows = self.state.db.find_stage_rows(ticker="BBB", trade_date="20250110")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["LoadId"], "manual_correction")
        self.assertEqual(self.state.data_errors.get(alert.data_error_id).resolved, 1)

    def test_add_row_validation_errors(self):
        form = {"action": "insert", "Ticker": "BBB", "TradeDate": "2025-01-10", "Open": "1", "High": "2",
                "Low": "0.5", "Close": "1.5", "Vol": "100"}
        r = self.client.post("/correct", data=form)
        self.assertEqual(r.status_code, 400)
        self.assertIn("calendar date", r.get_data(as_text=True))

    def test_correction_errors_from_service_are_shown(self):
        error, row = self.load_bad_high_low()
        from stock_dataload.correction import CorrectionError
        with mock.patch.object(self.state.corrections, "correct", side_effect=CorrectionError("db gone")):
            r = self.client.post("/correct", data={"action": "update", "stage_id": row["StageId"], "Market": "NYSE",
                                                   "Ticker": "AAA", "TradeDate": "20250110", "Open": "1",
                                                   "High": "2", "Low": "0.5", "Close": "1.5", "Vol": "100"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("db gone", r.get_data(as_text=True))
        with mock.patch.object(self.state.corrections, "add_row", side_effect=CorrectionError("db gone")):
            r = self.client.post("/correct", data={"action": "insert", "Ticker": "AAA", "TradeDate": "20250110",
                                                   "Open": "1", "High": "2", "Low": "0.5", "Close": "1.5",
                                                   "Vol": "100"})
        self.assertEqual(r.status_code, 400)

    def test_choose_page_from_data_error_with_several_rows(self):
        error, row = self.load_bad_high_low()
        # a second row with the same key in the same file (hand-added) -> user must choose
        self.state.corrections.add_row({"Market": "NYSE", "Ticker": "AAA", "TradeDate": "20250110", "Open": "1",
                                        "High": "2", "Low": "0.5", "Close": "1.5", "Vol": "100"},
                                       source_file="NYSE_20250110.txt")
        html = self.client.get(f"/correct?data_error_id={error.data_error_id}").get_data(as_text=True)
        self.assertIn("2 rows match", html)

    def test_corrections_json_and_recent_list(self):
        error, row = self.load_bad_high_low()
        self.state.corrections.correct(row["StageId"], dict(row, High="2"), note="fixed")
        data = self.client.get("/api/corrections").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["records"][0]["new_value"], "2")
        html = self.client.get("/correct").get_data(as_text=True)
        self.assertIn("Recent corrections", html)
        self.assertIn("fixed", html)

    def test_correction_pages_survive_unusable_database(self):
        self.state.settings.sqlite_path = self.root
        self.state.reload(self.state.settings)
        for url in ("/correct", "/correct?ticker=AAA", "/correct?data_error_id=1"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])


class AuthenticationTests(DashboardTestCase):
    def test_anonymous_is_redirected_to_login(self):
        self.logout()
        for url in ("/", "/formats", "/monitor", "/correct", "/settings", "/users", "/monitor/data-errors?alerts=1"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])
        self.assertIn("next=/monitor/data-errors?alerts%3D1",
                      self.client.get("/monitor/data-errors?alerts=1").headers["Location"])
        self.assertEqual(self.client.post("/load", data={}).status_code, 302)
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/logs").get_json()["error"], "authentication required")
        css = self.client.get("/static/style.css")
        self.assertEqual(css.status_code, 200)
        css.close()
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_login_logout_and_audit(self):
        self.logout()
        r = self.client.post("/login", data={"username": "viewer", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("unknown user name or wrong password", r.get_data(as_text=True))
        r = self.client.post("/login", data={"username": VIEWER[0], "password": VIEWER[1], "next": "/monitor"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/monitor"))
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("viewer", html)
        self.assertIn("Sign out", html)
        self.assertEqual(self.client.get("/login").status_code, 302)          # already signed in
        r = self.logout()
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302)
        events = [x.event for x in self.state.audit.fetch_logs(component="auth")]
        self.assertIn("login_failed", events)
        self.assertIn("login", events)
        self.assertIn("logout", events)

    def test_open_redirect_is_blocked(self):
        self.logout()
        r = self.client.post("/login", data={"username": VIEWER[0], "password": VIEWER[1], "next": "https://evil"})
        self.assertTrue(r.headers["Location"].endswith("/"))
        self.logout()
        r = self.client.post("/login", data={"username": VIEWER[0], "password": VIEWER[1], "next": "//evil"})
        self.assertTrue(r.headers["Location"].endswith("/"))

    def test_disabled_user_is_signed_out(self):
        self.logout()
        self.login(*VIEWER)
        self.users.update(self.users.get_by_username("viewer").user_id, active=False)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.post("/login", data={"username": VIEWER[0], "password": VIEWER[1]}).status_code, 401)

    def test_must_change_password_flow(self):
        user = self.users.create_user("newbie", "Temp-Pass-1", must_change_password=True)
        self.logout()
        r = self.client.post("/login", data={"username": "newbie", "password": "Temp-Pass-1"})
        self.assertTrue(r.headers["Location"].endswith("/account"))
        r = self.client.get("/monitor")                              # everything else is blocked
        self.assertTrue(r.headers["Location"].endswith("/account"))
        html = self.client.get("/account").get_data(as_text=True)
        self.assertIn("please choose your own", html)
        r = self.client.post("/account", data={"current_password": "Temp-Pass-1", "new_password": "My-Own-Pass-1",
                                               "confirm_password": "different"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/account", data={"current_password": "wrong", "new_password": "My-Own-Pass-1",
                                               "confirm_password": "My-Own-Pass-1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not correct", r.get_data(as_text=True))
        r = self.client.post("/account", data={"current_password": "Temp-Pass-1", "new_password": "My-Own-Pass-1",
                                               "confirm_password": "My-Own-Pass-1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Password changed", self.flashes(r))
        self.assertEqual(self.client.get("/monitor").status_code, 200)
        self.assertFalse(self.users.get(user.user_id).must_change_password)
        self.assertIn("password_changed", [x.event for x in self.state.audit.fetch_logs(component="auth")])


class AuthorizationTests(DashboardTestCase):
    def test_viewer_is_read_only(self):
        self.logout()
        self.login(*VIEWER)
        for url in ("/", "/formats", "/monitor", "/monitor/logs", "/monitor/errors", "/monitor/data-errors",
                    "/monitor/history", "/api/status", "/api/monitor", "/account"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/correct").status_code, 403)
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.get("/users").status_code, 403)
        self.assertEqual(self.client.get("/formats/new").status_code, 403)
        r = self.client.post("/load", data={})
        self.assertEqual(r.status_code, 403)
        self.assertIn("needs the <b>operator</b> role", r.get_data(as_text=True).replace("\n", " "))
        self.assertEqual(self.client.post("/convert-all", data={}).status_code, 403)
        self.assertEqual(self.client.post("/upload", data={}).status_code, 403)
        self.assertEqual(self.client.post("/formats/yahoo/delete").status_code, 403)
        self.assertEqual(self.client.post("/correct", data={}).status_code, 403)
        self.assertEqual(self.client.post("/users/new", data={}).status_code, 403)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="role-viewer"', html)
        self.assertNotIn("/settings", html)
        self.assertNotIn("Data correction", html)
        self.assertNotIn("+ New format", self.client.get("/formats").get_data(as_text=True))
        self.assertIn("access_denied", [x.event for x in self.state.audit.fetch_logs(component="auth")])

    def test_operator_can_operate_but_not_administer(self):
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.get("/correct").status_code, 200)
        self.assertEqual(self.client.get("/formats/new").status_code, 200)
        r = self.client.post("/load", data={})
        self.assertEqual(r.status_code, 302)                     # allowed (nothing to load -> redirect)
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.get("/users").status_code, 403)
        self.assertEqual(self.client.post("/users/new", data={"username": "x"}).status_code, 403)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Data correction", html)
        self.assertNotIn("/users", html)

    def test_api_forbidden_is_json(self):
        self.logout()
        self.login(*VIEWER)
        # no admin-only api exists today; the viewer may read the monitoring api
        self.assertEqual(self.client.get("/api/corrections").status_code, 200)


class UserManagementTests(DashboardTestCase):
    def test_users_page_lists_accounts(self):
        html = self.client.get("/users").get_data(as_text=True)
        for name in ("admin", "ops", "viewer"):
            self.assertIn(name, html)
        self.assertIn(">you<", html)

    def test_admin_adds_user(self):
        self.assertEqual(self.client.get("/users/new").status_code, 200)
        r = self.client.post("/users/new", data={"username": "jane", "display_name": "Jane Doe", "role": "operator",
                                                 "password": "Jane-Pass-1", "confirm_password": "Jane-Pass-1",
                                                 "must_change_password": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("created with role operator", self.flashes(r))
        jane = self.users.get_by_username("jane")
        self.assertEqual(jane.role, "operator")
        self.assertTrue(jane.must_change_password)
        self.assertEqual(jane.created_by, "admin")
        self.assertIn("user_created", [x.event for x in self.state.audit.fetch_logs(component="auth")])
        # the new user can sign in (and is sent to change the password)
        self.logout()
        r = self.client.post("/login", data={"username": "jane", "password": "Jane-Pass-1"})
        self.assertTrue(r.headers["Location"].endswith("/account"))

    def test_add_user_validation(self):
        r = self.client.post("/users/new", data={"username": "jane", "role": "viewer", "password": "Jane-Pass-1",
                                                 "confirm_password": "other"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("do not match", r.get_data(as_text=True))
        r = self.client.post("/users/new", data={"username": "admin", "role": "viewer", "password": "Jane-Pass-1",
                                                 "confirm_password": "Jane-Pass-1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already exists", r.get_data(as_text=True))
        r = self.client.post("/users/new", data={"username": "jane", "role": "viewer", "password": "short",
                                                 "confirm_password": "short"})
        self.assertEqual(r.status_code, 400)

    def test_edit_user_role_status_and_password(self):
        viewer = self.users.get_by_username("viewer")
        html = self.client.get(f"/users/{viewer.user_id}/edit").get_data(as_text=True)
        self.assertIn("Edit user viewer", html)
        r = self.client.post(f"/users/{viewer.user_id}/edit", data={"display_name": "View Er", "role": "operator",
                                                                    "active": "on"})
        self.assertEqual(r.status_code, 302)
        viewer = self.users.get(viewer.user_id)
        self.assertEqual((viewer.role, viewer.display_name), ("operator", "View Er"))
        # deactivate + reset password
        r = self.client.post(f"/users/{viewer.user_id}/edit", data={"role": "operator", "new_password": "Reset-Pass-1",
                                                                    "confirm_password": "Reset-Pass-1",
                                                                    "must_change_password": "on"})
        self.assertEqual(r.status_code, 302)
        viewer = self.users.get(viewer.user_id)
        self.assertFalse(viewer.is_active)
        self.assertTrue(viewer.must_change_password)
        self.assertIn("password_reset", [x.event for x in self.state.audit.fetch_logs(component="auth")])
        # validation errors
        r = self.client.post(f"/users/{viewer.user_id}/edit", data={"role": "operator", "new_password": "Reset-Pass-1",
                                                                    "confirm_password": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/users/9999/edit").status_code, 404)
        self.assertEqual(self.client.post("/users/9999/edit", data={}).status_code, 404)

    def test_admin_cannot_deactivate_self_or_last_admin(self):
        admin = self.users.get_by_username("admin")
        # the 'active' checkbox is ignored for the own account
        r = self.client.post(f"/users/{admin.user_id}/edit", data={"role": "admin"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.users.get(admin.user_id).is_active)
        r = self.client.post(f"/users/{admin.user_id}/edit", data={"role": "viewer", "active": "on"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("last active administrator", r.get_data(as_text=True))
        r = self.client.post(f"/users/{admin.user_id}/delete")
        self.assertIn("cannot delete your own account", self.flashes(r))

    def test_delete_user(self):
        ops = self.users.get_by_username("ops")
        r = self.client.post(f"/users/{ops.user_id}/delete")
        self.assertIn("deleted", self.flashes(r))
        self.assertIsNone(self.users.get(ops.user_id))
        self.assertEqual(self.client.post("/users/9999/delete").status_code, 404)
        # last-admin protection surfaces as a flash
        second = self.users.create_user("root2", "Root-Pass-2", role="admin")
        self.logout()
        self.login("root2", "Root-Pass-2")
        admin = self.users.get_by_username("admin")
        self.users.update(second.user_id, role="admin")
        self.users.update(admin.user_id, role="viewer")          # now root2 is the only admin
        self.logout()
        self.login("root2", "Root-Pass-2")
        r = self.client.post(f"/users/{second.user_id}/delete")
        self.assertIn("cannot delete your own account", self.flashes(r))


class BootstrapAdminTests(TempProjectTestCase):
    """With an empty user table the dashboard creates the initial administrator."""

    def make_app(self):
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        return create_app(load_settings(self.settings.config_file))

    def test_initial_admin_with_one_time_password(self):
        import contextlib
        import io
        import os
        import unittest.mock as mock
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_DATALOAD_ADMIN_PASSWORD", None)
            with contextlib.redirect_stdout(out):
                app = self.make_app()
        banner = out.getvalue()
        self.assertIn("Initial administrator created: username 'admin'", banner)
        self.assertIn("One-time password:", banner)
        password = banner.split("One-time password:")[1].split()[0]
        client = app.test_client()
        r = client.post("/login", data={"username": "admin", "password": password})
        self.assertTrue(r.headers["Location"].endswith("/account"))      # must change it first
        state = app.extensions["state"]
        self.assertIn("admin_bootstrapped", [x.event for x in state.audit.fetch_logs(component="auth")])
        # a second start does not create another one
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            create_app(load_settings(self.settings.config_file))
        self.assertEqual(out2.getvalue(), "")
        self.assertEqual(UserStore(self.settings).count(), 1)

    def test_initial_admin_password_from_environment(self):
        import contextlib
        import io
        import os
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"STOCK_DATALOAD_ADMIN_PASSWORD": "Env-Admin-Pass-1"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                app = self.make_app()
        self.assertIn("STOCK_DATALOAD_ADMIN_PASSWORD", out.getvalue())
        self.assertNotIn("Env-Admin-Pass-1", out.getvalue())
        r = app.test_client().post("/login", data={"username": "admin", "password": "Env-Admin-Pass-1"})
        self.assertEqual(r.status_code, 302)

    def test_bootstrap_can_be_disabled_and_survives_db_problems(self):
        import contextlib
        import io
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            create_app(load_settings(self.settings.config_file), bootstrap=False)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(UserStore(self.settings).count(), 0)
        s = load_settings(self.settings.config_file)
        s.sqlite_path = self.root                                 # unusable database
        s.secret_key_file = self.root / "k"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            create_app(s)                                          # no crash
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
